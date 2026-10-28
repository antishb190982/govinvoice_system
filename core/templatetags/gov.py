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
    from core.money import fmt

    return fmt(Decimal(value), currency or None, with_code=bool(currency))


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


# Line icons (24px grid, stroke = currentColor) – attributes only, so they satisfy the strict CSP.
ICONS = {
    "home": '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>',
    "file": '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/><path d="M9 13h6M9 17h6"/>',
    "scan": '<path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4"/><path d="M7 12h10"/>',
    "check": '<path d="M4 12l5 5L20 6"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "bank": '<path d="M3 10l9-6 9 6"/><path d="M5 10v8M9 10v8M15 10v8M19 10v8M3 20h18"/>',
    "chart": '<path d="M4 20V4"/><path d="M4 20h16"/><path d="M8 16v-5M12 16V8M16 16v-3"/>',
    "building": '<path d="M4 21V5l8-3v19"/><path d="M12 9h8v12"/><path d="M7 8h2M7 12h2M7 16h2M15 13h2M15 17h2"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
    "bell": '<path d="M6 16V11a6 6 0 1112 0v5l2 2H4z"/><path d="M10 20a2 2 0 004 0"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 5-6 8-6s6.5 2 8 6"/>',
    "logout": '<path d="M15 4h4v16h-4"/><path d="M10 8l-4 4 4 4M6 12h10"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "chevron": '<path d="M6 9l6 6 6-6"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "spark": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
}


@register.simple_tag
def icon(name, size=18, cls=""):
    from django.utils.safestring import mark_safe

    path = ICONS.get(name, "")
    return mark_safe(f'<svg class="icon {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
                     f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
                     f'aria-hidden="true">{path}</svg>')


@register.simple_tag
def brand_mark():
    from django.conf import settings
    from django.utils.html import format_html

    name, hi = settings.BRAND_NAME, settings.BRAND_HIGHLIGHT
    if hi and name.endswith(hi):
        return format_html('<span class="wordmark">{}<span class="wm-accent">{}</span></span>', name[: -len(hi)], hi)
    return format_html('<span class="wordmark">{}</span>', name)
