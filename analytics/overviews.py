"""Live scoped summaries whose links use the register's actual filter contract."""

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from analytics.sections import SECTIONS, records_url
from core.utils import require_any_role
from imports.models import ImportBatch
from invoices.models import Invoice
from operations.models import DisputeCase
from operations.workspace import FOLDERS, batches_for
from org.access import scope
from org.models import Role
from providers.models import ServiceAccount
from reconciliation.models import StatementRow
from settlements.models import SettlementInstruction
from workflow.engine import pending_for
from core.provider_scope import filter_provider


def _overview_filters(request):
    params = request.GET.copy()
    if "provider" not in params and getattr(request, "utility_provider", None):
        params["provider"] = str(request.utility_provider.pk)
    return params


def grouped(qs, field):
    return dict(qs.order_by().values(field).annotate(n=Count("pk")).values_list(field, "n"))


def data_for(section, user, params=None):
    """No mutation, no unrestricted financial joins, and no mixed-currency sum."""
    now = timezone.now()
    cards, breakdown, providers, amounts = [], [], [], []
    subtitle = "Current records in your authorised units. Counts include all dates; open a figure to inspect its records."
    account_params = {}
    if section == "accounts" and params is not None:
        account_params = {key: value for key, value in params.items()
                          if key in {"q", "provider", "ministry", "service_type", "status", "invoicing_status"} and value}
    def url(**extra):
        target = dict(account_params)
        target.update(extra)
        return records_url(section, **target)

    def card(label, value, params=None, note="", tone="info"):
        cards.append({"label": label, "value": value, "url": url(**(params or {})), "note": note, "tone": tone})

    def statuses(qs, choices, field="status", param="status"):
        counts = grouped(qs, field)
        for value, label in choices:
            breakdown.append({"label": label, "count": counts.get(value, 0), "url": url(**{param: value})})
        return counts

    if section in {"invoices", "analytics"}:
        invoices = scope(Invoice.objects.all(), user)
        # Historical analysis has its own month/cohort filters; operational
        # snapshot links intentionally go to the exact invoice register instead.
        url = lambda **params: records_url("invoices", **params)
        counts = statuses(invoices, Invoice.Status.choices)
        card("All invoices", sum(counts.values()), note="All statuses, including preparation.")
        card("Awaiting approval", counts.get("in_approval", 0), {"status": "in_approval"})
        overdue = invoices.filter(status__in=Invoice.UNPAID, due_date__lt=timezone.localdate())
        card("Overdue unpaid", overdue.count(), {"status": "open", "due_to": (timezone.localdate() -
             timedelta(days=1)).isoformat()}, "Due before today; excludes draft and paid invoices.", "warn")
        card("Flagged invoices", invoices.exclude(flags=[]).count(), {"flagged": "on"}, "Recorded checks requiring review.", "warn")
        for row in invoices.order_by().values("provider_id", "provider__name").annotate(count=Count("pk")).order_by("-count", "provider__name")[:10]:
            providers.append({"label": row["provider__name"], "count": row["count"], "url": url(provider=row["provider_id"])})
        billed = invoices.exclude(status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED])
        for row in billed.order_by().values("currency").annotate(total=Sum("amount_total"), count=Count("pk")).order_by("currency"):
            amounts.append({**row, "url": url(currency=row["currency"], status="billed")})
        if section == "analytics":
            subtitle = ("All-date operational snapshot for your authorised units. Drill into invoices below; "
                        "Detailed analysis provides separate historical month and comparison filters.")
    elif section == "batches":
        qs = batches_for(user)
        counts = grouped(qs, "batch_folder")
        for key, label in FOLDERS.items():
            if key != "all":
                count = counts.get(key, 0)
                breakdown.append({"label": label, "count": count, "url": url(folder=key)})
        card("All batches", sum(counts.values()))
        for key in ("preparation", "work", "rejected"):
            card(FOLDERS[key], counts.get(key, 0), {"folder": key}, tone="warn" if key == "rejected" else "info")
    elif section == "imports":
        require_any_role(user, [Role.CLERK, Role.UNIT_ADMIN])
        qs = filter_provider(ImportBatch.objects.all() if user.is_superuser else ImportBatch.objects.filter(uploaded_by=user))
        counts = statuses(qs, ImportBatch.Status.choices)
        subtitle = "Your uploaded intake batches only; system administrators can inspect all imports. Counts include all dates."
        card("All intake batches", sum(counts.values()))
        for status, label in ImportBatch.Status.choices:
            card(label, counts.get(status, 0), {"status": status})
    elif section == "approvals":
        tasks = pending_for(user).filter(invoice__batch_memberships__active=True)
        batch_key = "invoice__batch_memberships__batch_id"
        count_batches = lambda rows: rows.order_by().values(batch_key).distinct().count()
        subtitle = "Pending approval work is grouped by complete processing batch; invoice evidence remains available in each batch."
        card("Approval batches", count_batches(tasks))
        card("Overdue batches", count_batches(tasks.filter(due_at__lt=now)), {"attention": "overdue"}, "At least one approval deadline has passed.", "warn")
        card("Expedited batches", count_batches(tasks.filter(expedited=True)), {"attention": "expedited"})
        counts = {role: count_batches(tasks.filter(role=role)) for role, _ in Role.choices}
        for role, label in Role.choices:
            if role in counts:
                breakdown.append({"label": label, "count": counts[role], "url": url(role=role)})
    elif section == "settlements":
        qs = scope(SettlementInstruction.objects.all(), user)
        counts = statuses(qs, SettlementInstruction.Status.choices)
        card("All settlement instructions", sum(counts.values()))
        for state, label in [("draft", "In preparation"), ("pending", "Awaiting authorisation"), ("sent", "Awaiting execution")]:
            card(label, counts.get(state, 0), {"status": state}, tone="warn" if state == "pending" else "info")
    elif section == "reconciliation":
        qs = scope(StatementRow.objects.filter(statement__is_active=True), user, prefix="assigned_unit__")
        counts = statuses(qs, StatementRow.Status.choices, param="state")
        card("Active statement rows", sum(counts.values()), note="Only active provider statements; includes ledger-only exceptions.")
        for state, label in StatementRow.Status.choices:
            card(label, counts.get(state, 0), {"state": state}, tone="warn" if state == "unsorted" else "info")
    elif section == "bank":
        from reconciliation import bank
        from reconciliation.bank_views import rows_for
        from reconciliation.models import BankRow

        require_any_role(user, bank.READERS)
        qs = rows_for(user)
        counts = statuses(qs, BankRow.Status.choices)
        card("Bank statement transactions", sum(counts.values()))
        for state, label in BankRow.Status.choices:
            card(label, counts.get(state, 0), {"status": state}, tone="warn" if state == "exception" else "info")
        subtitle = "Imported bank debit rows within your finance access. Provider acceptance or an executed instruction alone is not bank reconciliation."
    elif section == "accounts":
        from providers.account_filters import dashboard_for
        dashboard = dashboard_for(user, params)
        subtitle = "Filtered account counts and invoicing status. Choose an account filter first, then drill into the matching register rows."
        for item in dashboard["account_dashboard_cards"]:
            card(item["label"], item["count"])
            cards[-1]["url"] = item["url"]
        breakdown.extend({"label": row["label"], "count": row["count"], "url": row["url"]}
                         for row in dashboard["account_status_breakdown"])
    elif section == "disputes":
        from operations.services import LIVE_CASES

        qs = scope(DisputeCase.objects.all(), user)
        counts = statuses(qs, DisputeCase.Status.choices)
        card("All dispute cases", sum(counts.values()))
        card("Open action cases", qs.filter(status__in=LIVE_CASES).count(), {"status": "active"}, tone="warn")
        card("Awaiting authorisation", counts.get("authorisation", 0), {"status": "authorisation"})
        card("Refund outstanding", counts.get("refund_due", 0), {"status": "refund_due"}, tone="warn")
    return {"cards": cards, "breakdown": breakdown, "providers": providers, "amounts": amounts,
            "subtitle": subtitle}


@login_required
@require_GET
@never_cache
def overview(request, section):
    if section not in SECTIONS:
        raise Http404
    return render(request, "analytics/overview.html", {
        "section_title": SECTIONS[section][0], "records_url": records_url(section),
        "generated_at": timezone.now(), **data_for(section, request.user, _overview_filters(request)),
    })
