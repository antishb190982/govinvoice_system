from decimal import Decimal

from django import template
from django.utils import timezone
from django.utils.html import format_html

from invoices.models import Flag

register = template.Library()

STATUS_TONES = {
    "draft": "neutral", "in_approval": "info", "returned": "warn", "approved": "ok", "scheduled": "info",
    "paid": "ok", "disputed": "danger", "cancelled": "neutral", "pending": "warn", "authorised": "ok",
    "sent": "info", "executed": "ok", "waiting": "neutral", "staged": "info", "committed": "ok",
    "discarded": "neutral", "valid": "ok", "invalid": "danger", "skipped": "neutral", "rejected": "danger",
}


@register.filter
def money(value, currency=""):
    if value in (None, ""):
        return "—"
    amount = f"{Decimal(value):,.3f}"
    return f"{currency} {amount}".strip()


@register.filter
def compact(value):
    if value in (None, ""):
        return "—"
    v = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= limit:
            return f"{v / limit:,.1f}{suffix}"
    return f"{v:,.0f}"


@register.simple_tag
def status_badge(obj, field="status"):
    value = getattr(obj, field)
    label = getattr(obj, f"get_{field}_display")()
    return format_html('<span class="badge badge-{}">{}</span>', STATUS_TONES.get(value, "neutral"), label)


@register.filter
def flag_label(code):
    return Flag.LABELS.get(code, code)


@register.filter
def flag_severe(code):
    return code in Flag.SEVERE


@register.simple_tag
def sla_badge(task):
    if not task.due_at:
        return ""
    now = timezone.now()
    if task.decided_at:
        late = task.decided_at > task.due_at
        return format_html('<span class="badge badge-{}">{}</span>', "danger" if late else "ok",
                           "Decided late" if late else "On time")
    delta = task.due_at - now
    hours = delta.total_seconds() / 3600
    if hours < 0:
        return format_html('<span class="badge badge-danger">Overdue {}h</span>', int(-hours) or "<1")
    tone = "warn" if (task.remaining_fraction() or 0) < 0.25 else "info"
    text = f"{int(hours)}h left" if hours < 48 else f"{int(hours // 24)}d left"
    return format_html('<span class="badge badge-{}">{}</span>', tone, text)


@register.filter
def days_until(value):
    if not value:
        return ""
    return (value - timezone.localdate()).days


@register.simple_tag(takes_context=True)
def sort_link(context, field, label):
    request = context["request"]
    params = request.GET.copy()
    current = params.get("sort", "")
    params["sort"] = f"-{field}" if current == field else field
    params.pop("page", None)
    arrow = " ↑" if current == field else (" ↓" if current == f"-{field}" else "")
    return format_html('<a href="?{}">{}{}</a>', params.urlencode(), label, arrow)


@register.filter
def get_item(mapping, key):
    return mapping.get(key) if mapping else None
