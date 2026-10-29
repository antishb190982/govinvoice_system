"""Whole-batch settlement grouping; draft instructions reserve all member invoices."""

from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q

from invoices.models import Invoice
from operations.models import InvoiceBatch
from operations.services import ministry_for, payable, require_clear
from org.access import can_view, has_role, scope
from org.models import Role
from reconciliation.services import require_reconciled


def instructions_for(batch):
    from settlements.models import SettlementInstruction

    return SettlementInstruction.objects.filter(
        Q(batch=batch) | Q(batch_links__batch=batch)).distinct()


def validate_batch(batch, user, org_unit, provider, currency):
    if not has_role(user, org_unit, [Role.FINANCE]) or not can_view(user, batch.org_unit):
        raise PermissionDenied("You need finance access to every selected batch.")
    if not batch.org_unit.path.startswith(org_unit.path):
        raise ValidationError("Every batch must belong to the selected paying unit.")
    if batch.cancelled_at:
        raise ValidationError(f"{batch.reference}: the batch is cancelled.")
    if batch.provider_id != provider.pk or batch.currency != currency:
        raise ValidationError("A settlement group must use one utility provider and one currency.")
    if instructions_for(batch).exclude(status="cancelled").exists():
        raise ValidationError(f"{batch.reference}: already reserved by a live settlement.")
    members = list(batch.members.filter(active=True).select_related("invoice__org_unit"))
    if not members:
        raise ValidationError(f"{batch.reference}: the batch has no active invoices.")
    invoices = []
    for member in members:
        invoice = member.invoice
        if (invoice.provider_id != provider.pk or invoice.currency != currency or
                ministry_for(invoice.org_unit).pk != batch.ministry_id or
                not invoice.org_unit.path.startswith(org_unit.path)):
            raise ValidationError(f"{batch.reference}: invoice provider, currency or ministry has changed.")
        amount = payable(invoice)
        if amount == 0 and invoice.status == Invoice.Status.PAID:
            continue
        if amount <= 0 or invoice.status != Invoice.Status.APPROVED:
            raise ValidationError(f"{batch.reference}: every payable invoice must be approved.")
        require_clear(invoice)
        require_reconciled(invoice)
        invoices.append(invoice)
    if not invoices:
        raise ValidationError(f"{batch.reference}: no approved payable invoices remain.")
    return invoices


def ready_batches(user, org_unit, provider, currency):
    """Selection preview only. POST revalidates inside the creation transaction."""
    rows = []
    batches = scope(InvoiceBatch.objects.filter(
        cancelled_at__isnull=True, provider=provider, currency=currency,
        org_unit__path__startswith=org_unit.path).select_related("ministry", "provider", "org_unit"), user)
    for batch in batches:
        try:
            invoices = validate_batch(batch, user, org_unit, provider, currency)
        except (ValidationError, PermissionDenied):
            continue
        rows.append({"batch": batch, "invoice_count": len(invoices),
                     "amount": sum((payable(i) for i in invoices), Decimal("0")),
                     "earliest_due": min(i.due_date for i in invoices)})
    return rows


def selected_batches(user, org_unit, provider, currency, invoices, batch_ids):
    """Called after locking invoices; lock batches in a deterministic order too."""
    ids = {int(pk) for pk in batch_ids}
    batches = list(InvoiceBatch.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    if not ids or {b.pk for b in batches} != ids:
        raise ValidationError("Select at least one valid approved batch.")
    expected = set()
    for batch in batches:
        expected.update(i.pk for i in validate_batch(batch, user, org_unit, provider, currency))
    if len({batch.ministry_id for batch in batches}) != 1:
        raise ValidationError("A settlement group must belong to one ministry.")
    if expected != {i.pk for i in invoices}:
        raise ValidationError("A group must include every approved payable invoice from each selected batch.")
    return batches


def create_group(user, org_unit, provider, currency, batch_ids, notes=""):
    from operations.models import BatchMember
    from settlements.services import create_instruction

    # Creation acquires row locks and validates this selection again. A stale
    # browser selection must never produce a partial group.
    invoice_ids = list(BatchMember.objects.filter(batch_id__in=batch_ids, active=True)
                       .exclude(invoice__status=Invoice.Status.PAID).values_list("invoice_id", flat=True))
    due = Invoice.objects.filter(pk__in=invoice_ids).order_by("-due_date").values_list("due_date", flat=True).first()
    if not due:
        raise ValidationError("Select at least one approved payable batch.")
    return create_instruction(user, org_unit, currency, due, invoice_ids, notes,
                              provider=provider, batch_ids=batch_ids)


def check_group(instruction):
    """Validate frozen membership as well as invoice amounts at every release gate."""
    if not instruction.group_reference:
        return
    links = list(instruction.batch_links.select_related("batch"))
    items = list(instruction.lines.values(
        "items__invoice_id", "items__batch_id", "items__amount", "provider_id"))
    if not links or instruction.batch_id or len({link.batch.ministry_id for link in links}) != 1:
        raise ValidationError("Settlement group membership is inconsistent. Cancel and rebuild it.")
    batch_ids = {link.batch_id for link in links}
    if {item["items__batch_id"] for item in items} != batch_ids:
        raise ValidationError("Settlement group invoice membership no longer matches its batches.")
    if len({link.batch.provider_id for link in links}) != 1 or any(
            link.batch.currency != instruction.currency for link in links):
        raise ValidationError("Settlement group provider or currency is inconsistent.")
    for link in links:
        selected = [item for item in items if item["items__batch_id"] == link.batch_id]
        if (len(selected) != link.invoice_count or
                sum((item["items__amount"] for item in selected), Decimal("0")) != link.amount or
                any(item["provider_id"] != link.batch.provider_id for item in selected)):
            raise ValidationError("Settlement group batch amounts no longer match their invoices.")
    if sum((link.amount for link in links), Decimal("0")) != instruction.total_amount:
        raise ValidationError("Settlement group total no longer matches its member batches.")
