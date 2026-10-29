from decimal import Decimal
import hashlib
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from core.models import AuditEvent, Counter
from invoices.models import Invoice
from operations.models import BatchMember, DisputeCase, InvoiceBatch, RefundReceipt
from org.access import has_role
from org.models import Role

PROCESSORS = [Role.CLERK, Role.UNIT_ADMIN, Role.FINANCE]
OPENERS = PROCESSORS + [Role.VERIFIER, Role.APPROVER, Role.AUTHORISER]
LIVE_CASES = ["open", "authorisation", "refund_due"]


def document_hash(document):
    if not document:
        return ""
    if document.size > 10 * 1024 * 1024:
        raise ValidationError("Supporting evidence must be at most 10 MB.")
    digest = hashlib.sha256()
    for chunk in document.chunks():
        digest.update(chunk)
    document.seek(0)
    return digest.hexdigest()


def ministry_for(unit):
    current = unit
    while current and current.kind != "ministry":
        current = current.parent
    if not current:
        raise ValidationError("Invoices must belong to a ministry before they can enter a batch.")
    return current


def payable(invoice):
    waived = invoice.dispute_cases.filter(applies_payable=True).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    return invoice.amount_total - waived


def require_clear(invoice):
    fee, reviewed = Invoice.objects.filter(pk=invoice.pk).values_list("surcharge_amount", "surcharge_reviewed_at").get()
    fee_waived = invoice.dispute_cases.filter(is_surcharge=True, applies_payable=True).aggregate(
        total=Sum("amount"))["total"] or Decimal("0")
    if fee > fee_waived and not reviewed:
        raise ValidationError(f"{invoice.reference}: surcharge needs a finance review before approval or settlement.")
    from providers.models import ServiceAccount
    status, enabled = ServiceAccount.objects.filter(pk=invoice.service_account_id).values_list("status", "is_active").get()
    if status != "active" or not enabled:
        raise ValidationError(f"{invoice.reference}: service account is {status.replace('_', ' ')} or archived. A ministry administrator must review it before processing.")
    if payable(invoice) < 0:
        raise ValidationError("The payable amount cannot be negative after internal adjustments.")
    if invoice.dispute_cases.filter(status__in=LIVE_CASES).exists():
        raise ValidationError(f"{invoice.reference} is on dispute hold. Resolve its open case before approval or settlement.")


def log(action, obj, user, **data):
    return AuditEvent.record(action, obj=obj, actor=user, org_unit_id=obj.org_unit_id, data=data)


@transaction.atomic
def create_batch(user, org_unit, name, invoice_ids):
    if not has_role(user, org_unit, PROCESSORS):
        raise PermissionDenied
    invoices = list(Invoice.objects.select_for_update().filter(pk__in=invoice_ids).order_by("pk"))
    if not invoices or {i.pk for i in invoices} != {int(pk) for pk in invoice_ids}:
        raise ValidationError("Select valid invoices for this batch.")
    if any(not i.org_unit.path.startswith(org_unit.path) for i in invoices):
        raise PermissionDenied("Every invoice must be inside your selected unit.")
    ministries = {ministry_for(i.org_unit).pk for i in invoices}
    if len(ministries) != 1 or len({i.provider_id for i in invoices}) != 1 or len({i.currency for i in invoices}) != 1:
        raise ValidationError("A batch must contain one ministry, one utility provider and one currency.")
    if any(i.status in [Invoice.Status.PAID, Invoice.Status.CANCELLED,
                         Invoice.Status.SCHEDULED] for i in invoices):
        raise ValidationError("Paid, closed, cancelled or already scheduled invoices cannot enter a new batch.")
    if BatchMember.objects.filter(invoice__in=invoices, active=True).exists():
        raise ValidationError("An invoice already belongs to an active batch.")
    ministry = ministry_for(invoices[0].org_unit)
    batch = InvoiceBatch.objects.create(reference=Counter.next("BATCH"), name=name.strip()[:140],
        org_unit=org_unit if org_unit.path.startswith(ministry.path) else ministry,
        ministry=ministry, provider=invoices[0].provider, currency=invoices[0].currency, created_by=user)
    for invoice in invoices:
        BatchMember.objects.create(batch=batch, invoice=invoice, original_amount=payable(invoice))
    log("batch.created", batch, user, ministry=ministry.code, provider=batch.provider.short_code,
        invoices=[i.reference for i in invoices], amount=str(sum(payable(i) for i in invoices)))
    return batch


def selection_batch(user, org_unit, invoices):
    """Legacy/API settlement selection also becomes one explicit, whole batch."""
    memberships = list(BatchMember.objects.filter(invoice__in=invoices, active=True).select_related("batch"))
    batches = {member.batch_id for member in memberships}
    if memberships:
        if len(batches) != 1 or len(memberships) != len(invoices):
            raise ValidationError("Select exactly one complete batch; do not mix batched and unbatched invoices.")
        batch = memberships[0].batch
        if any(i.provider_id != batch.provider_id or i.currency != batch.currency or
               ministry_for(i.org_unit).pk != batch.ministry_id for i in invoices):
            raise ValidationError("Batch provider, ministry or currency no longer matches its invoices. Release and regroup them.")
        active_members = list(batch.members.filter(active=True).select_related("invoice"))
        zero_members = [m.invoice for m in active_members if payable(m.invoice) <= 0]
        if any(i.status != "paid" for i in zero_members):
            raise ValidationError("Complete approval of zero-payable members before preparing settlement.")
        cash_ids = {m.invoice_id for m in active_members if payable(m.invoice) > 0}
        if cash_ids != {i.pk for i in invoices}:
            raise ValidationError("Settlement must include the entire batch and its full approved amount.")
        if batch.cancelled_at:
            raise ValidationError("This batch is cancelled.")
        return batch
    return create_batch(user, org_unit, "Settlement batch", [i.pk for i in invoices])


@transaction.atomic
def start_batch(batch, user):
    from workflow.engine import submit
    invoices = list(Invoice.objects.select_for_update().filter(
        batch_memberships__batch=batch, batch_memberships__active=True).order_by("pk"))
    batch = InvoiceBatch.objects.select_for_update().get(pk=batch.pk)
    if not has_role(user, batch.org_unit, [Role.CLERK, Role.UNIT_ADMIN]):
        raise PermissionDenied
    if batch.cancelled_at or not invoices:
        raise ValidationError("Cancelled or empty batches cannot be processed.")
    submitted = []
    for invoice in invoices:
        if invoice.status == "paid" and payable(invoice) == 0:
            continue
        require_clear(invoice)
        if invoice.status in [Invoice.Status.DRAFT, Invoice.Status.RETURNED, Invoice.Status.DISPUTED]:
            submit(invoice, user)
            submitted.append(invoice.reference)
        elif invoice.status not in [Invoice.Status.APPROVED, Invoice.Status.IN_APPROVAL]:
            raise ValidationError("Every member must be ready for processing; no partial submission was made.")
    batch.started_at = batch.started_at or timezone.now()
    batch.save(update_fields=["started_at"])
    log("batch.submitted", batch, user, submitted=submitted)
    return batch


@transaction.atomic
def cancel_batch(batch, user, reason):
    list(Invoice.objects.select_for_update().filter(batch_memberships__batch=batch).order_by("pk"))
    batch = InvoiceBatch.objects.select_for_update().get(pk=batch.pk)
    if not has_role(user, batch.org_unit, PROCESSORS):
        raise PermissionDenied
    if batch.started_at or batch.cancelled_at or batch.settlements.exclude(status="cancelled").exists():
        raise ValidationError("Only an unstarted batch without a live settlement can be cancelled.")
    if not reason.strip():
        raise ValidationError("Explain why this batch is cancelled.")
    batch.cancelled_at = timezone.now()
    batch.save(update_fields=["cancelled_at"])
    batch.members.update(active=False)
    log("batch.cancelled", batch, user, reason=reason[:2000])


@transaction.atomic
def open_case(user, invoice, kind, reason, amount=Decimal("0"), item_description="", item_amount=None, evidence=None, is_surcharge=False):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if not has_role(user, invoice.org_unit, OPENERS):
        raise PermissionDenied
    if kind not in DisputeCase.Kind.values or not reason.strip() or len(reason) > 4000:
        raise ValidationError("Select a case type and provide a reason of at most 4,000 characters.")
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount < 0 or (kind != "issue" and amount <= 0):
        raise ValidationError("A waiver/refund requires a positive amount.")
    limit = invoice.amount_total
    if is_surcharge:
        if invoice.surcharge_amount <= 0:
            raise ValidationError("Record the surcharge shown on this invoice before opening a surcharge case.")
        item_description, item_amount = "Invoice surcharge", invoice.surcharge_amount
    if item_description:
        if item_amount is None or item_amount <= 0 or item_amount > limit:
            raise ValidationError("Identify the item and its valid invoiced amount.")
        limit = item_amount
    elif item_amount is not None:
        raise ValidationError("Describe the invoice item to which this amount belongs.")
    if amount > limit:
        raise ValidationError("The case amount cannot exceed the invoice or selected item amount.")
    evidence_sha = document_hash(evidence)
    case = DisputeCase.objects.create(reference=Counter.next("CASE"), invoice=invoice, org_unit=invoice.org_unit,
        is_surcharge=is_surcharge,
        kind=kind, reason=reason, amount=amount, item_description=item_description[:250], item_amount=item_amount,
        evidence=evidence or "", opened_by=user, invoice_snapshot=invoice.snapshot())
    log("dispute.opened", case, user, invoice=invoice.reference, stage=invoice.status,
        kind=kind, amount=str(amount), item=item_description, evidence_sha256=evidence_sha)
    return case


@transaction.atomic
def endorse(case, user, note):
    case = DisputeCase.objects.select_for_update().get(pk=case.pk)
    if not has_role(user, case.org_unit, [Role.FINANCE]):
        raise PermissionDenied
    if case.status not in ["open", "authorisation"] or not note.strip():
        raise ValidationError("Provide a finance assessment for an open case.")
    case.finance_by, case.finance_note, case.status = user, note[:2000], "authorisation"
    from reconciliation.services import fingerprint
    case.review_fingerprint = fingerprint(Invoice.objects.select_for_update().get(pk=case.invoice_id))
    case.save()
    log("dispute.finance_reviewed", case, user, note=case.finance_note)
    return case


@transaction.atomic
def decide_case(case, user, approve, note, pin):
    from signatures.services import check_pin
    from workflow.models import ApprovalTask
    case = DisputeCase.objects.select_for_update().get(pk=case.pk)
    if not has_role(user, case.org_unit, [Role.AUTHORISER]) or user.pk in [case.opened_by_id, case.finance_by_id]:
        raise PermissionDenied("An independent payment authoriser must decide this case.")
    if case.status != "authorisation" or not note.strip():
        raise ValidationError("This case needs a finance review and a decision reason.")
    check_pin(user, pin)
    list(ApprovalTask.objects.select_for_update().filter(invoice_id=case.invoice_id,
        status__in=["pending", "waiting"]).order_by("pk"))
    invoice = Invoice.objects.select_for_update().get(pk=case.invoice_id)
    from reconciliation.services import fingerprint
    if approve and case.review_fingerprint != fingerprint(invoice):
        raise ValidationError("Invoice changed since finance review. Finance must refresh its review before authorisation.")
    if approve and case.kind in ["waiver", "refund"]:
        if case.item_description:
            prior = sum((other.amount for other in invoice.dispute_cases.filter(
                kind__in=["waiver", "refund"], status__in=["resolved", "refund_due", "refunded"]).exclude(pk=case.pk)
                if other.item_description.strip().casefold() == case.item_description.strip().casefold()), Decimal("0"))
            if prior + case.amount > case.item_amount:
                raise ValidationError("Approved adjustments exceed the original amount of this invoice item.")
        if invoice.status == Invoice.Status.CANCELLED:
            raise ValidationError("Cancelled invoices can be investigated, but cannot receive a payable adjustment.")
        if invoice.status == Invoice.Status.PAID:
            reserved = invoice.dispute_cases.filter(status__in=["refund_due", "refunded"]).aggregate(
                total=Sum("amount"))["total"] or Decimal("0")
            if case.amount > payable(invoice) - reserved:
                raise ValidationError("This exceeds the remaining refundable amount.")
            case.status = "refund_due"
        elif case.kind == "refund":
            raise ValidationError("A refund needs a recorded payment. Withdraw this request or resolve an unpaid bill through a waiver.")
        else:
            if invoice.settlement_items.exclude(line__instruction__status="cancelled").exists():
                raise ValidationError("Cancel the live settlement instruction before applying a waiver.")
            if case.amount > payable(invoice):
                raise ValidationError("This exceeds the remaining payable amount.")
            case.applies_payable, case.status = True, "resolved"
            if invoice.status not in [Invoice.Status.DRAFT, Invoice.Status.RETURNED]:
                invoice.status, invoice.approved_at = Invoice.Status.RETURNED, None
                invoice.save(update_fields=["status", "approved_at", "updated_at"])
                ApprovalTask.objects.filter(invoice=invoice, status__in=["pending", "waiting"]).update(status="cancelled")
    else:
        case.status = "resolved" if approve else "rejected"
        if invoice.status == Invoice.Status.DISPUTED:
            invoice.status = Invoice.Status.RETURNED
            invoice.save(update_fields=["status", "updated_at"])
    case.approved_by, case.decided_at, case.decision_note = user, timezone.now(), note[:2000]
    case.save()
    if case.applies_payable:
        from reconciliation.services import invoice_saved
        invoice_saved(invoice)
    log("dispute.authorised" if approve else "dispute.rejected", case, user,
        amount=str(case.amount), applies_payable=case.applies_payable, note=case.decision_note,
        original_payment_preserved=invoice.status == Invoice.Status.PAID)
    return case


@transaction.atomic
def record_refund(case, user, reference, received_date, document, amount):
    case = DisputeCase.objects.select_for_update().get(pk=case.pk)
    if not has_role(user, case.org_unit, [Role.FINANCE]):
        raise PermissionDenied
    if case.status != "refund_due" or not reference.strip() or not document or received_date > timezone.localdate():
        raise ValidationError("Record an approved refund with its bank reference, receipt date and supporting document.")
    received = case.refund_receipts.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    if amount <= 0 or received + amount > case.amount:
        raise ValidationError("Receipt exceeds the remaining approved refund, or is not positive.")
    evidence_sha = document_hash(document)
    try:
        with transaction.atomic():
            receipt = RefundReceipt.objects.create(case=case, amount=amount, reference=reference[:120],
                received_date=received_date, document=document, recorded_by=user)
    except IntegrityError as exc:
        raise ValidationError("This refund transaction reference has already been recorded.") from exc
    case.refund_reference, case.refund_date, case.refund_document = reference[:120], received_date, receipt.document.name
    case.refund_by = user
    case.status = "refunded" if received + amount == case.amount else "refund_due"
    case.save()
    log("dispute.refund_received", case, user, amount=str(amount), bank_reference=case.refund_reference,
        received_date=str(received_date), receipt_id=receipt.pk, remaining=str(case.amount - received - amount),
        evidence_sha256=evidence_sha)
    return case


@transaction.atomic
def withdraw(case, user, reason):
    case = DisputeCase.objects.select_for_update().get(pk=case.pk)
    if not has_role(user, case.org_unit, OPENERS):
        raise PermissionDenied
    if not has_role(user, case.org_unit, [Role.FINANCE, Role.UNIT_ADMIN]) and user.pk != case.opened_by_id:
        raise PermissionDenied
    if case.status not in ["open", "authorisation"] or not reason.strip():
        raise ValidationError("Only an undecided case can be withdrawn, with a reason.")
    case.status = "withdrawn"
    case.save(update_fields=["status"])
    log("dispute.withdrawn", case, user, reason=reason[:2000])
