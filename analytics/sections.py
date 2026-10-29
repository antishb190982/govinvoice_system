"""Navigation metadata only; no data queries during template navigation."""

from urllib.parse import urlencode

from django.urls import reverse


SECTIONS = {
    "invoices": ("Invoices", "invoices:list", "Invoice register"),
    "batches": ("Batch workspace", "operations:workspace", "Batch folders"),
    "imports": ("Invoice intake", "imports:list", "Imports"),
    "approvals": ("My approvals", "workflow:inbox", "Approval queue"),
    "settlements": ("Settlements", "settlements:list", "Settlement register"),
    "reconciliation": ("Provider reconciliation", "reconciliation:index", "Statement records"),
    "bank": ("Bank reconciliation", "reconciliation:bank_index", "Bank records"),
    "accounts": ("Service accounts", "providers:accounts", "Account register"),
    "disputes": ("Disputes and adjustments", "operations:cases", "Dispute basket"),
    "analytics": ("Historical insights", "analytics", "Detailed analysis"),
}


def records_url(section, **params):
    if section == "batches":
        params.setdefault("folder", "all")
    elif section == "reconciliation":
        params.setdefault("state", "all")
    elif section in {"bank", "disputes"}:
        params.setdefault("status", "all")
    url = reverse(SECTIONS[section][1])
    return url + ("?" + urlencode(params) if params else "")


def section_navigation(request):
    match = request.resolver_match
    if not match:
        return {}
    active = "records"
    section = next((key for key, (_, route, _) in SECTIONS.items() if match.view_name == route), None)
    if match.view_name == "operations:batches":
        section = "batches"
    if match.view_name == "core:section_overview":
        section, active = match.kwargs.get("section"), "overview"
    if section not in SECTIONS:
        return {}
    title, route, label = SECTIONS[section]
    ctx = {"section_tabs": {
        "title": title, "overview_url": reverse("core:section_overview", args=[section]),
        "records_url": records_url(section), "records_label": label, "active": active,
    }}
    if active == "overview":
        app, _, name = route.rpartition(":")
        ctx.update(nav_app=app, nav_url=name)
    return ctx
