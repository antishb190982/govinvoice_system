from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.control import ControlFilter
from core.utils import paginate
from core.work_queries import anomalies, label, overdue_sources, personal_tasks, row_for, schedules_for


class WorkFilter(ControlFilter):
    assignment = None


TABS = {"tasks": "Assigned tasks", "schedules": "Processing schedules",
        "anomalies": "Submitted anomalies", "overdue": "Overdue in my units"}


def link(params, **changes):
    query = params.copy()
    query.pop("page", None)
    for key, value in changes.items():
        query[key] = value
    return reverse("core:my_work") + "?" + query.urlencode()


def overdue_count(key, qs):
    if key == "settlement":
        return qs.filter(Q(pay_by__lt=timezone.localdate()) | Q(
            status="pending", authorise_by__lt=timezone.now())).count()
    fields = {"preparation": "due_date", "invoice_anomalies": "due_date",
              "bank": "resolution_due_date", "bank_anomalies": "resolution_due_date",
              "approval": "operation_due", "settlement": "pay_by", "schedules": "next_due_date"}
    field = fields.get(key)
    if not field:
        return 0
    if key == "schedules":
        qs = qs.filter(is_active=True)
    if key == "invoice_anomalies":
        from invoices.models import Invoice
        qs = qs.filter(status__in=Invoice.UNPAID | {Invoice.Status.DRAFT})
    if key in {"preparation", "invoice_anomalies"}:
        return qs.filter(Q(due_date__lt=timezone.localdate()) | Q(
            status__in=["returned", "in_approval"], must_approve_by__lt=timezone.now())).count()
    return qs.filter(**{field + "__lt": timezone.now() if key == "approval" else timezone.localdate()}).count()


@login_required
def my_work(request):
    params = request.GET.copy()
    current_provider = getattr(request, "utility_provider", None)
    if "provider" in params and not params["provider"] and current_provider:
        params["clear_provider"] = "1"
        return redirect(link(params))
    if "provider" not in params and current_provider:
        params["provider"] = current_provider.pk
        return redirect(link(params))
    tab, mode = params.get("tab", "tasks"), params.get("mode", "dashboard")
    if tab not in TABS or mode not in {"dashboard", "list"}:
        raise Http404("Unknown personal work view")
    form = WorkFilter(params, user=request.user)
    context = {"filter_form": form, "tab": tab, "mode": mode, "cards": [], "rows": [],
        "page": None, "category": params.get("category", ""), "total_count": 0,
        "tabs": [{"key": key, "label": title, "url": link(params, tab=key, category="")}
                 for key, title in TABS.items()],
        "modes": [{"key": key, "label": title, "url": link(params, mode=key)}
                  for key, title in [("dashboard", "Dashboard"), ("list", "Listing")]],
        "scope_note": ("Current overdue work in your authorised units plus your own schedules. Categories may refer to the same invoice."
            if tab == "overdue" else "Your assigned work and workflow-role responsibilities. Submitted anomalies retain current and historical findings."
            if tab == "anomalies" else "Your own schedules and work assigned to you or your workflow roles. Scheduling never approves or pays bills.")}
    if form.is_valid():
        builders = {"tasks": personal_tasks, "schedules": lambda user, data: {"schedules": schedules_for(user, data)},
                    "anomalies": anomalies, "overdue": overdue_sources}
        queues = builders[tab](request.user, form.cleaned_data)
        category = context["category"]
        if category and category not in queues:
            raise Http404("Unknown personal work category")
        counts = {key: qs.count() for key, qs in queues.items()}
        if tab == "overdue":
            request._work_overdue_counts = (form.cleaned_data, counts)
        context["cards"] = [{"key": key, "label": label(key), "count": counts[key],
            "overdue": counts[key] if tab == "overdue" else overdue_count(key, qs),
            "url": link(params, mode="list", category=key)} for key, qs in queues.items()]
        selected = {category: queues[category]} if category else queues
        context["total_count"] = sum(counts[key] for key in selected)
        if mode == "list" and category:
            page = paginate(request, selected[category], 30)
            page.object_list = [row_for(category, obj) for obj in page.object_list]
            context.update(rows=page.object_list, page=page)
        elif mode == "list":
            # Select only the current page across queue counts, without loading all records.
            page = paginate(request, range(context["total_count"]), 30)
            start, stop = (page.number - 1) * 30, page.number * 30
            offset, rows = 0, []
            for key, qs in selected.items():
                end = offset + counts[key]
                if start < end and stop > offset:
                    rows.extend(row_for(key, obj) for obj in qs[max(0, start-offset):max(0, stop-offset)])
                offset = end
            page.object_list = rows
            context.update(rows=rows, page=page)
        else:
            context["rows"] = [row_for(key, obj) for key, qs in selected.items() for obj in qs[:5]]
            context["rows"].sort(key=lambda row: not row["overdue"])
    return render(request, "core/my_work.html", context, status=400 if form.errors else 200)


def dashboard_overdue(request):
    match = request.resolver_match
    is_dashboard = match and (match.view_name in {
        "home", "core:dashboard", "core:control_centre", "core:section_overview",
        "core:my_work", "core:my_space", "analytics", "providers:visibility", "providers:accounts",
    } or (match.app_name in {"operations", "reconciliation", "imports", "workflow", "settlements", "providers"}
           and match.url_name in {"index", "workspace", "batch_list", "list", "bank_index", "bank", "disputes"}))
    if not is_dashboard:
        return None
    params = request.GET.copy()
    if not params.get("provider") and getattr(request, "utility_provider", None):
        params["provider"] = request.utility_provider.pk
    form = WorkFilter(params, user=request.user)
    if not form.is_valid():
        return None
    cached = getattr(request, "_work_overdue_counts", None)
    if cached and cached[0] == form.cleaned_data:
        counts = cached[1]
    else:
        counts = {key: qs.count() for key, qs in overdue_sources(request.user, form.cleaned_data).items()}
    cards = [{"label": label(key), "count": count,
        "url": link(params, tab="overdue", mode="list", category=key)} for key, count in counts.items()]
    return {"cards": cards, "total_count": sum(card["count"] for card in cards),
        "url": link(params, tab="overdue", mode="list", category=""),
        "scope_note": f"Current overdue work as of {timezone.localtime():%d %b %Y %H:%M}, within authorised units and selected ministry/provider. Includes draft processing deadlines, not just approved liabilities. Schedules are yours only; historical report periods do not change this live view."}
