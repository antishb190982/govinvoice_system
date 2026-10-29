"""Receipt-date anomalies and live deadline-based processing priority."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def assess_dates(issue_date, due_date, received_date, recorded_date=None, now=None):
    from invoices.services import approval_deadline

    now = now or timezone.now()
    today = timezone.localdate(now)
    received_date = received_date or recorded_date or today
    flags, notes = [], []
    if received_date > today:
        flags.append("future_receipt")
        notes.append("Receipt date is in the future.")
    if issue_date and received_date < issue_date:
        flags.append("receipt_before_issue")
        notes.append("Receipt date precedes the invoice issue date.")
    if due_date and received_date > due_date:
        flags.append("received_overdue")
        notes.append("Invoice was already overdue when received.")
    if recorded_date and (recorded_date - received_date).days > 7:
        flags.append("recording_delay")
        notes.append(f"Recorded {(recorded_date - received_date).days} days after receipt.")
    deadline = approval_deadline(due_date) if due_date else None
    if deadline and received_date > timezone.localdate(deadline):
        flags.append("late_arrival")
        notes.append("Received after the normal approval deadline.")
    if due_date and due_date < today:
        priority, label = "overdue", "Overdue"
    elif deadline and now >= deadline:
        priority, label = "urgent", "Urgent"
    elif due_date and due_date <= today + timedelta(days=settings.DUE_SOON_DAYS):
        priority, label = "high", "High"
    else:
        priority, label = "normal", "Normal"
    return {"flags": flags, "notes": notes, "priority": priority, "label": label, "approve_by": deadline}


def with_processing_priority(queryset):
    from django.db.models import Case, IntegerField, Q, Value, When
    from invoices.models import Invoice

    today = timezone.localdate()
    return queryset.annotate(processing_rank=Case(
        When(~Q(status__in=Invoice.OPEN) | Q(amount_total=0), then=Value(0)),
        When(due_date__lt=today, then=Value(4)),
        When(must_approve_by__lte=timezone.now(), then=Value(3)),
        When(due_date__lte=today + timedelta(days=settings.DUE_SOON_DAYS), then=Value(2)),
        default=Value(1), output_field=IntegerField(),
    ))
