import hashlib
import uuid
from collections import defaultdict
from datetime import timedelta
from xml.etree import ElementTree as ET

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from core.models import AuditEvent, Counter, Notification
from core.notify import notify
from core.worktime import end_of_working_day, subtract_working_days
from invoices.models import Invoice
from org.access import has_role, users_with_role
from org.models import Role
from settlements import pdf as pdf_render
from settlements.models import SettlementBatch, SettlementEvent, SettlementInstruction, SettlementItem, SettlementLine
from signatures.services import active_identity, check_pin, sign_pdf


def eligible_invoices(org_unit, currency, cutoff_date):
    candidates = Invoice.objects.filter(
        status=Invoice.Status.APPROVED, currency=currency, due_date__lte=cutoff_date, amount_total__gt=0,
        service_account__status="active", service_account__is_active=True,
        org_unit__path__startswith=org_unit.path,
    ).select_related("provider", "service_account", "org_unit").order_by("due_date")
    from reconciliation.services import report
    from operations.services import LIVE_CASES, payable
    candidates = candidates.exclude(dispute_cases__status__in=LIVE_CASES)
    return candidates.filter(pk__in=[inv.pk for inv in candidates if payable(inv) > 0 and report(inv)["valid"]])


def preview(org_unit, currency, cutoff_date, invoice_ids=None, provider=None):
    from operations.services import payable
    invoices = eligible_invoices(org_unit, currency, cutoff_date)
    if provider:
        invoices = invoices.filter(provider=provider)
    if invoice_ids is not None:
        invoices = invoices.filter(pk__in=invoice_ids)
    groups = defaultdict(list)
    for inv in invoices:
        groups[inv.provider].append(inv)
    lines = []
    for provider, items in sorted(groups.items(), key=lambda kv: min(i.due_date for i in kv[1])):
        for invoice in items:
            invoice.payable_amount = payable(invoice)
        lines.append({"provider": provider, "invoices": items, "amount": sum(payable(i) for i in items),
                      "earliest_due": min(i.due_date for i in items),
                      "missing_bank": not (provider.bank_iban and provider.bank_account_name)})
    return lines


def _execution_date(pay_by):
    """Ask the bank to execute one working day before the earliest due date (never in the past)."""
    target = subtract_working_days(pay_by, 1)
    return max(target, timezone.localdate())


@transaction.atomic
def create_instruction(user, org_unit, currency, cutoff_date, invoice_ids, notes="", provider=None, batch_ids=None):
    if not has_role(user, org_unit, [Role.FINANCE]):
        raise PermissionDenied("You need the finance officer role for this unit.")
    invoices = list(eligible_invoices(org_unit, currency, cutoff_date).filter(pk__in=invoice_ids)
                    .order_by("pk").select_for_update(of=("self",)))
    if not invoices:
        raise ValidationError("Select at least one approved invoice.")
    if {inv.pk for inv in invoices} != {int(pk) for pk in invoice_ids}:
        raise ValidationError("Some selected invoices are no longer approved and reconciled. Refresh the selection; no partial instruction was created.")
    from reconciliation.services import require_reconciled
    from operations.services import ministry_for, payable, require_clear, selection_batch
    if len({inv.provider_id for inv in invoices}) != 1 or len({ministry_for(inv.org_unit).pk for inv in invoices}) != 1:
        raise ValidationError("A settlement batch is limited to one utility provider and one ministry.")
    if provider and any(inv.provider_id != provider.pk for inv in invoices):
        raise ValidationError("Selected invoices do not belong to the chosen provider.")
    for inv in invoices:
        require_clear(inv)
        require_reconciled(inv)
    groups = defaultdict(list)
    for inv in invoices:
        groups[inv.provider].append(inv)
    for provider in groups:
        if not (provider.bank_iban and provider.bank_account_name):
            raise ValidationError(f"{provider.name} has no beneficiary bank details. Update the provider first.")
    pay_by = min(i.due_date for i in invoices)
    from settlements.grouping import instructions_for, selected_batches
    if batch_ids is not None:
        batches = selected_batches(user, org_unit, invoices[0].provider, currency, invoices, batch_ids)
        batch = None
    else:
        batch = selection_batch(user, org_unit, invoices)
        batches = [batch]
    for member_batch in batches:
        if instructions_for(member_batch).exclude(status="cancelled").exists():
            raise ValidationError("This batch already has a live settlement instruction.")
        if not member_batch.started_at:
            member_batch.started_at = timezone.now()
            member_batch.save(update_fields=["started_at"])
    instruction = SettlementInstruction.objects.create(
        batch=batch,
        group_reference=Counter.next("SG") if batch_ids is not None else None,
        reference=Counter.next("SI"), org_unit=org_unit, currency=currency, cutoff_date=cutoff_date,
        pay_by=pay_by, requested_execution_date=_execution_date(pay_by), notes=notes,
        payer_name=settings.PAYER_NAME, payer_iban=settings.PAYER_IBAN, payer_bic=settings.PAYER_BIC,
        prepared_by=user, total_amount=sum(payable(i) for i in invoices),
    )
    from operations.models import BatchMember
    member_map = dict(BatchMember.objects.filter(
        batch__in=batches, active=True, invoice__in=invoices).values_list("invoice_id", "batch_id"))
    for member_batch in batches:
        batch_invoices = [i for i in invoices if member_map.get(i.pk) == member_batch.pk]
        if batch_ids is not None:
            SettlementBatch.objects.create(instruction=instruction, batch=member_batch,
                amount=sum(payable(i) for i in batch_invoices), invoice_count=len(batch_invoices))
    for provider, items in groups.items():
        line = SettlementLine.objects.create(
            instruction=instruction, provider=provider, amount=sum(payable(i) for i in items),
            invoice_count=len(items), earliest_due=min(i.due_date for i in items),
            beneficiary_name=provider.bank_account_name, beneficiary_iban=provider.bank_iban,
            beneficiary_bic=provider.bank_bic, end_to_end_id=f"{instruction.reference}-{provider.short_code}"[:35],
        )
        for inv in items:
            SettlementItem.objects.create(line=line, invoice=inv, amount=payable(inv), batch_id=member_map.get(inv.pk))
    Invoice.objects.filter(pk__in=[i.pk for i in invoices]).update(status=Invoice.Status.SCHEDULED)
    AuditEvent.record("settlement.created", obj=instruction, actor=user, data={
        "reference": instruction.reference, "batch": batch.reference if batch else None,
        "group": instruction.group_reference, "batches": [b.reference for b in batches],
        "total": instruction.total_amount, "invoices": [i.reference for i in invoices]})
    if instruction.group_reference:
        AuditEvent.record("settlement.group_created", obj=instruction, actor=user, org_unit_id=org_unit.pk,
            data={"group": instruction.group_reference, "batches": [b.reference for b in batches],
                  "provider": invoices[0].provider.short_code, "currency": currency, "total": instruction.total_amount})
        for member_batch in batches:
            AuditEvent.record("batch.settlement_grouped", obj=member_batch, actor=user,
                org_unit_id=member_batch.org_unit_id,
                data={"group": instruction.group_reference, "instruction": instruction.reference,
                      "amount": instruction.batch_links.get(batch=member_batch).amount})
    from reconciliation.services import check_instruction
    check_instruction(instruction)
    return instruction


def _store_pdf(instruction, data):
    if instruction.pdf:
        instruction.pdf.delete(save=False)
    instruction.pdf.save(f"{instruction.reference}.pdf", ContentFile(data), save=False)
    instruction.pdf_sha256 = hashlib.sha256(data).hexdigest()


def _signing_box(data, label):
    return pdf_render.SIGNATURE_BOXES[label], pdf_render.page_count(data) - 1


def submit_for_authorisation(instruction, user, pin):
    check_pin(user, pin)
    return _submit_for_authorisation(instruction, user, pin)


@transaction.atomic
def _submit_for_authorisation(instruction, user, pin):
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status != SettlementInstruction.Status.DRAFT:
        raise ValidationError("Only a draft instruction can be submitted.")
    if user != instruction.prepared_by and not has_role(user, instruction.org_unit, [Role.FINANCE]):
        raise PermissionDenied
    from reconciliation.services import check_instruction
    check_instruction(instruction)
    data = pdf_render.render(instruction)
    box, page = _signing_box(data, "Prepared")
    signed = sign_pdf(user, pin, data, "Prepared", reason=f"Prepared settlement instruction {instruction.reference}",
                      location=instruction.org_unit.name, box=box, page=page)
    _store_pdf(instruction, signed)
    now = timezone.now()
    instruction.status = SettlementInstruction.Status.PENDING_AUTHORISATION
    instruction.submitted_at = now
    instruction.prepared_by = user
    instruction.rejection_comment = ""
    # Authorisation must land by the end of the working day before execution.
    instruction.authorise_by = end_of_working_day(subtract_working_days(instruction.requested_execution_date, 1)) \
        if instruction.requested_execution_date > timezone.localdate() else end_of_working_day(timezone.localdate())
    instruction.save()
    SettlementEvent.objects.create(instruction=instruction, role="prepared", user=user,
                                   identity=active_identity(user), pdf_sha256=instruction.pdf_sha256)
    AuditEvent.record("settlement.submitted", obj=instruction, actor=user, data={"pdf_sha256": instruction.pdf_sha256})
    notify(users_with_role(instruction.org_unit, [Role.AUTHORISER]).exclude(pk=user.pk),
           f"Authorise settlement {instruction.reference}",
           f"{instruction.currency} {instruction.total_amount:,} to {instruction.lines.count()} providers, "
           f"pay by {instruction.pay_by:%d %b %Y}. Authorise by {timezone.localtime(instruction.authorise_by):%d %b %H:%M}.",
           link=instruction.get_absolute_url(), level=Notification.Level.WARNING,
           dedupe_key=f"settlement-{instruction.pk}-submitted-{int(now.timestamp())}")
    return instruction


def authorise(instruction, user, pin):
    if user.pk == instruction.prepared_by_id:
        raise PermissionDenied("Segregation of duties: the preparer cannot authorise.")
    check_pin(user, pin)
    return _authorise(instruction, user, pin)


@transaction.atomic
def _authorise(instruction, user, pin):
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status != SettlementInstruction.Status.PENDING_AUTHORISATION:
        raise ValidationError("This instruction is not awaiting authorisation.")
    if not has_role(user, instruction.org_unit, [Role.AUTHORISER]):
        raise PermissionDenied("You need the payment authoriser role for this unit.")
    if user.pk == instruction.prepared_by_id:
        raise PermissionDenied("Segregation of duties: the preparer cannot authorise.")
    from reconciliation.services import check_instruction
    check_instruction(instruction)
    instruction.pdf.open("rb")
    data = instruction.pdf.read()
    instruction.pdf.close()
    if hashlib.sha256(data).hexdigest() != instruction.pdf_sha256:
        raise ValidationError("The stored PDF does not match its recorded hash; it may have been tampered with.")
    box, page = _signing_box(data, "Authorised")
    signed = sign_pdf(user, pin, data, "Authorised", reason=f"Authorised payment of {instruction.reference}",
                      location=instruction.org_unit.name, box=box, page=page)
    _store_pdf(instruction, signed)
    instruction.status = SettlementInstruction.Status.AUTHORISED
    instruction.authorised_by = user
    instruction.authorised_at = timezone.now()
    instruction.save()
    SettlementEvent.objects.create(instruction=instruction, role="authorised", user=user,
                                   identity=active_identity(user), pdf_sha256=instruction.pdf_sha256)
    AuditEvent.record("settlement.authorised", obj=instruction, actor=user, data={"pdf_sha256": instruction.pdf_sha256})
    notify([instruction.prepared_by], f"{instruction.reference} authorised",
           "Export the payment file and send it to the bank/treasury.", link=instruction.get_absolute_url(),
           dedupe_key=f"settlement-{instruction.pk}-authorised")
    return instruction


@transaction.atomic
def reject(instruction, user, comment):
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status != SettlementInstruction.Status.PENDING_AUTHORISATION:
        raise ValidationError("This instruction is not awaiting authorisation.")
    if not has_role(user, instruction.org_unit, [Role.AUTHORISER]) or user.pk == instruction.prepared_by_id:
        raise PermissionDenied
    if not comment.strip():
        raise ValidationError("Give a reason.")
    instruction.status = SettlementInstruction.Status.DRAFT
    instruction.rejection_comment = comment.strip()
    instruction.save()
    AuditEvent.record("settlement.rejected", obj=instruction, actor=user, data={"comment": comment})
    notify([instruction.prepared_by], f"{instruction.reference} returned", comment,
           link=instruction.get_absolute_url(), level=Notification.Level.WARNING,
           dedupe_key=f"settlement-{instruction.pk}-rejected-{int(timezone.now().timestamp())}")
    return instruction


@transaction.atomic
def cancel(instruction, user, reason):
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status not in (SettlementInstruction.Status.DRAFT, SettlementInstruction.Status.PENDING_AUTHORISATION):
        raise ValidationError("Only draft or pending instructions can be cancelled.")
    if not has_role(user, instruction.org_unit, [Role.FINANCE, Role.AUTHORISER]):
        raise PermissionDenied
    ids = list(SettlementItem.objects.filter(line__instruction=instruction).values_list("invoice_id", flat=True))
    Invoice.objects.filter(pk__in=ids, status=Invoice.Status.SCHEDULED).update(status=Invoice.Status.APPROVED)
    instruction.status = SettlementInstruction.Status.CANCELLED
    instruction.rejection_comment = reason
    instruction.save()
    AuditEvent.record("settlement.cancelled", obj=instruction, actor=user, data={"reason": reason})


@transaction.atomic
def mark_sent(instruction, user):
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status != SettlementInstruction.Status.AUTHORISED:
        raise ValidationError("Only an authorised instruction can be sent.")
    if not has_role(user, instruction.org_unit, [Role.FINANCE, Role.AUTHORISER]):
        raise PermissionDenied
    from reconciliation.services import check_instruction
    check_instruction(instruction)
    instruction.reconciliation_evidence.update(released_at=timezone.now())
    instruction.status = SettlementInstruction.Status.SENT
    instruction.sent_at = timezone.now()
    instruction.save()
    AuditEvent.record("settlement.sent", obj=instruction, actor=user)


@transaction.atomic
def mark_executed(instruction, user, bank_reference, value_date):
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status not in (SettlementInstruction.Status.AUTHORISED, SettlementInstruction.Status.SENT):
        raise ValidationError("Only an authorised instruction can be marked as executed.")
    if not has_role(user, instruction.org_unit, [Role.FINANCE, Role.AUTHORISER]):
        raise PermissionDenied
    if not bank_reference.strip():
        raise ValidationError("Enter the bank / treasury reference.")
    from reconciliation.services import check_instruction
    check_instruction(instruction, released=instruction.status == SettlementInstruction.Status.SENT)
    instruction.status = SettlementInstruction.Status.EXECUTED
    instruction.bank_reference = bank_reference.strip()
    instruction.executed_at = value_date
    instruction.save()
    ids = list(SettlementItem.objects.filter(line__instruction=instruction).values_list("invoice_id", flat=True))
    Invoice.objects.filter(pk__in=ids).update(status=Invoice.Status.PAID, paid_at=value_date,
                                              payment_reference=instruction.bank_reference)
    late = Invoice.objects.filter(pk__in=ids, due_date__lt=value_date).count()
    AuditEvent.record("settlement.executed", obj=instruction, actor=user,
                      data={"bank_reference": instruction.bank_reference, "value_date": value_date, "paid_late": late})


# --- Bank file ------------------------------------------------------------------

def _amount(value, currency):
    from core.money import quantize

    return f"{quantize(value, currency)}"


def pain001(instruction):
    from reconciliation.services import check_instruction
    with transaction.atomic():
        check_instruction(instruction, released=instruction.status in [
            SettlementInstruction.Status.SENT, SettlementInstruction.Status.EXECUTED])
    """ISO 20022 customer credit transfer initiation (pain.001.001.09)."""
    ns = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
    ET.register_namespace("", ns)

    def el(parent, tag, text=None):
        node = ET.SubElement(parent, f"{{{ns}}}{tag}")
        if text is not None:
            node.text = str(text)
        return node

    lines = list(instruction.lines.select_related("provider").prefetch_related("items__invoice"))
    root = ET.Element(f"{{{ns}}}Document")
    cst = el(root, "CstmrCdtTrfInitn")
    hdr = el(cst, "GrpHdr")
    el(hdr, "MsgId", f"{instruction.reference}-{uuid.uuid4().hex[:8]}"[:35])
    el(hdr, "CreDtTm", timezone.localtime().replace(microsecond=0).isoformat())
    el(hdr, "NbOfTxs", len(lines))
    el(hdr, "CtrlSum", _amount(instruction.total_amount, instruction.currency))
    el(el(hdr, "InitgPty"), "Nm", instruction.payer_name[:140])

    pmt = el(cst, "PmtInf")
    el(pmt, "PmtInfId", instruction.reference)
    el(pmt, "PmtMtd", "TRF")
    el(pmt, "NbOfTxs", len(lines))
    el(pmt, "CtrlSum", _amount(instruction.total_amount, instruction.currency))
    el(el(pmt, "ReqdExctnDt"), "Dt", instruction.requested_execution_date.isoformat())
    el(el(pmt, "Dbtr"), "Nm", instruction.payer_name[:140])
    el(el(el(pmt, "DbtrAcct"), "Id"), "IBAN", (instruction.payer_iban or "").replace(" ", ""))
    agent = el(el(pmt, "DbtrAgt"), "FinInstnId")
    if instruction.payer_bic:
        el(agent, "BICFI", instruction.payer_bic)
    el(pmt, "ChrgBr", "SLEV")
    for line in lines:
        tx = el(pmt, "CdtTrfTxInf")
        el(el(tx, "PmtId"), "EndToEndId", line.end_to_end_id)
        amt = el(el(tx, "Amt"), "InstdAmt", _amount(line.amount, instruction.currency))
        amt.set("Ccy", instruction.currency)
        if line.beneficiary_bic:
            el(el(el(tx, "CdtrAgt"), "FinInstnId"), "BICFI", line.beneficiary_bic)
        el(el(tx, "Cdtr"), "Nm", line.beneficiary_name[:140])
        el(el(el(tx, "CdtrAcct"), "Id"), "IBAN", (line.beneficiary_iban or "").replace(" ", ""))
        refs = ", ".join(item.invoice.invoice_number for item in line.items.all())
        el(el(tx, "RmtInf"), "Ustrd", f"{instruction.reference}: {refs}"[:140])
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def sweep_due(now=None):
    """Alert finance about approved invoices nearing their due date without an instruction,
    and authorisers about instructions close to their authorisation deadline."""
    today = timezone.localdate()
    horizon = today + timedelta(days=settings.DUE_SOON_DAYS)
    alerted = 0
    for inv in Invoice.objects.filter(status=Invoice.Status.APPROVED, due_date__lte=horizon).select_related(
            "org_unit", "provider"):
        level = Notification.Level.CRITICAL if inv.due_date <= today + timedelta(days=2) else Notification.Level.WARNING
        alerted += notify(users_with_role(inv.org_unit, [Role.FINANCE]),
                          f"Unscheduled invoice due {inv.due_date:%d %b}: {inv.reference}",
                          f"{inv.provider.name} {inv.currency} {inv.amount_total:,} is approved but not yet in a "
                          "settlement instruction.", link=inv.get_absolute_url(), level=level,
                          dedupe_key=f"invoice-{inv.pk}-unscheduled-{level}")
    for si in SettlementInstruction.objects.filter(status=SettlementInstruction.Status.PENDING_AUTHORISATION,
                                                   authorise_by__lte=timezone.now() + timedelta(hours=4)):
        alerted += notify(users_with_role(si.org_unit, [Role.AUTHORISER]).exclude(pk=si.prepared_by_id),
                          f"Authorisation deadline: {si.reference}",
                          f"Authorise by {timezone.localtime(si.authorise_by):%d %b %H:%M} to pay by {si.pay_by:%d %b}.",
                          link=si.get_absolute_url(), level=Notification.Level.CRITICAL,
                          dedupe_key=f"settlement-{si.pk}-deadline")
    return alerted
