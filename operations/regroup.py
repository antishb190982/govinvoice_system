from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from invoices.models import Invoice
from operations.models import BatchMember, InvoiceBatch
from operations.services import PROCESSORS, log, payable
from org.access import has_role


@transaction.atomic
def release_member(batch, user, invoice_id, reason):
    """Regroup an unpaid member; retain original membership and approval history."""
    invoice = Invoice.objects.select_for_update().filter(pk=invoice_id).first()
    batch = InvoiceBatch.objects.select_for_update().get(pk=batch.pk)
    if not invoice or not has_role(user, batch.org_unit, PROCESSORS) or not has_role(user, invoice.org_unit, PROCESSORS):
        raise PermissionDenied
    member = BatchMember.objects.filter(batch=batch, invoice=invoice, active=True).first()
    if not member or not reason.strip() or batch.cancelled_at:
        raise ValidationError("Choose an active member and give a reason for releasing it.")
    if invoice.status in ["paid", "scheduled"] or batch.payment_instructions.exclude(status="cancelled").exists():
        raise ValidationError("Cancel the live settlement first; paid memberships cannot be released.")
    member.active = False
    member.save(update_fields=["active"])
    log("batch.member_released", batch, user, invoice=invoice.reference, amount=str(payable(invoice)),
        reason=reason[:4000], invoice_status=invoice.status)
    return member
