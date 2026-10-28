"""Invoice intake, validation and anomaly detection."""

from decimal import Decimal
from statistics import mean, pstdev

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import AuditEvent, Counter
from core.worktime import end_of_working_day, subtract_working_days
from invoices.models import Flag, Invoice

TOLERANCE = Decimal("0.01")
HISTORY_SIZE = 6


def approval_deadline(due_date):
    """Approvals must close SETTLEMENT_LEAD_DAYS working days before the due date."""
    return end_of_working_day(subtract_working_days(due_date, settings.SETTLEMENT_LEAD_DAYS))


def validate_business_rules(invoice):
    errors = {}
    if invoice.service_account_id and invoice.provider_id and \
            invoice.service_account.provider_id != invoice.provider_id:
        errors["service_account"] = "This account belongs to a different provider."
    if invoice.due_date and invoice.issue_date and invoice.due_date < invoice.issue_date:
        errors["due_date"] = "The due date is before the issue date."
    if invoice.period_start and invoice.period_end and invoice.period_end < invoice.period_start:
        errors["period_end"] = "The period ends before it starts."
    if invoice.issue_date and invoice.issue_date > timezone.localdate():
        errors["issue_date"] = "The issue date is in the future."
    if invoice.provider_id and invoice.invoice_number:
        clash = Invoice.objects.filter(provider_id=invoice.provider_id,
                                       invoice_number__iexact=invoice.invoice_number.strip())
        if invoice.pk:
            clash = clash.exclude(pk=invoice.pk)
        if clash.exists():
            errors["invoice_number"] = f"Invoice {invoice.invoice_number} from this provider is already recorded " \
                                       f"({clash.first().reference})."
    if errors:
        raise ValidationError(errors)


def detect_flags(invoice):
    flags = set()
    pct = Decimal(settings.ANOMALY_THRESHOLD_PCT) / 100

    if invoice.amount_net or invoice.amount_tax:
        if abs((invoice.amount_net or 0) + (invoice.amount_tax or 0) - invoice.amount_total) > TOLERANCE:
            flags.add(Flag.TOTAL_MISMATCH)

    if invoice.previous_reading is not None and invoice.current_reading is not None \
            and invoice.consumption is not None:
        if abs((invoice.current_reading - invoice.previous_reading) - invoice.consumption) > Decimal("0.5"):
            flags.add(Flag.READING_MISMATCH)

    history = Invoice.objects.filter(service_account_id=invoice.service_account_id) \
        .exclude(status=Invoice.Status.CANCELLED)
    if invoice.pk:
        history = history.exclude(pk=invoice.pk)

    recent = list(history.order_by("-issue_date")[:HISTORY_SIZE])
    if len(recent) >= 3:
        for attr, flag in (("amount_total", Flag.AMOUNT_SPIKE), ("consumption", Flag.CONSUMPTION_SPIKE)):
            values = [float(getattr(i, attr)) for i in recent if getattr(i, attr) is not None]
            current = getattr(invoice, attr)
            if len(values) >= 3 and current is not None:
                avg, sd = mean(values), pstdev(values)
                limit = max(avg * (1 + float(pct)), avg + 3 * sd)
                if avg > 0 and float(current) > limit:
                    flags.add(flag)

    if invoice.period_start and invoice.period_end:
        if history.filter(period_start__lte=invoice.period_end, period_end__gte=invoice.period_start).exists():
            flags.add(Flag.PERIOD_OVERLAP)
        if history.filter(period_start=invoice.period_start, period_end=invoice.period_end,
                          amount_total=invoice.amount_total).exists():
            flags.add(Flag.POSSIBLE_DUPLICATE)

    if invoice.document_sha256:
        same = Invoice.objects.filter(document_sha256=invoice.document_sha256)
        if invoice.pk:
            same = same.exclude(pk=invoice.pk)
        if invoice.import_batch_id:
            # Rows of one statement legitimately share its file.
            same = same.exclude(import_batch_id=invoice.import_batch_id)
        if same.exists():
            flags.add(Flag.SAME_DOCUMENT)

    if invoice.due_date and timezone.now() > approval_deadline(invoice.due_date):
        flags.add(Flag.LATE_ARRIVAL)
    return sorted(flags)


def prepare(invoice):
    """Derive copied fields, validate, flag. Does not save."""
    account = invoice.service_account
    invoice.provider = account.provider
    invoice.org_unit = account.org_unit if not invoice.org_unit_id or invoice.is_editable else invoice.org_unit
    invoice.utility_type = account.provider.utility_type
    invoice.invoice_number = invoice.invoice_number.strip()
    if not invoice.currency:
        invoice.currency = account.provider.currency or settings.DEFAULT_CURRENCY
    if not invoice.consumption_unit:
        invoice.consumption_unit = account.provider.consumption_unit
    if invoice.amount_total is None:
        invoice.amount_total = (invoice.amount_net or 0) + (invoice.amount_tax or 0)
    validate_business_rules(invoice)
    invoice.must_approve_by = approval_deadline(invoice.due_date)
    invoice.flags = detect_flags(invoice)
    return invoice


@transaction.atomic
def save_invoice(invoice, user, audit_action=None, changed=None):
    creating = invoice.pk is None
    if not creating and not invoice.is_editable:
        raise ValidationError("This invoice can no longer be edited.")
    prepare(invoice)
    if creating:
        invoice.reference = Counter.next("INV")
        invoice.created_by = user
    invoice.save()
    AuditEvent.record(audit_action or ("invoice.created" if creating else "invoice.updated"), obj=invoice,
                      actor=user, data={"reference": invoice.reference, "amount": invoice.amount_total,
                                        "flags": invoice.flags, "fields": changed or []})
    return invoice


def mark_overdue_flags():
    """Daily: flag unpaid invoices past their due date."""
    today = timezone.localdate()
    count = 0
    for inv in Invoice.objects.filter(status__in=Invoice.UNPAID, due_date__lt=today):
        if Flag.OVERDUE not in inv.flags:
            inv.flags = sorted(set(inv.flags) | {Flag.OVERDUE})
            Invoice.objects.filter(pk=inv.pk).update(flags=inv.flags)
            count += 1
    return count
