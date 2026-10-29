"""A private hub; links and counts never grant additional financial access."""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from core.models import SpaceFolder
from core.my_work import WorkFilter
from core.space_folders import folder_link
from core.utils import paginate
from core.work_queries import apply_scope, personal_tasks, row_for
from invoices.models import Invoice
from operations.workspace import batches_for
from org.access import scope
from org.models import Role
from providers.models import Provider, ServiceAccount
from workflow.engine import pending_for


TABS = {"overview": "Overview", "providers": "Utility providers", "batches": "Batches",
        "invoices": "Invoices", "services": "Services", "tasks": "Assigned tasks", "folders": "My folders"}


def space_url(params, **changes):
    query = params.copy()
    query.pop("page", None)
    for key, value in changes.items():
        query[key] = value
    return reverse("core:my_space") + "?" + query.urlencode()


def space_sources(user, data, tasks):
    batches = apply_scope(batches_for(user), data)
    role_batches = scope(batches.filter(members__active=True,
        members__invoice__status__in=["approved", "scheduled"]), user,
        [Role.FINANCE, Role.AUTHORISER, Role.UNIT_ADMIN])
    batches = batches.filter(
        Q(created_by=user) | Q(members__active=True, members__invoice__processing_owner=user) |
        Q(pk__in=tasks["approval"].values("pk")) | Q(pk__in=role_batches.values("pk")) |
        Q(members__active=True, members__invoice_id__in=tasks["settlement"].values("lines__items__invoice_id"))
    ).distinct().order_by("-created_at", "-pk")
    invoices = apply_scope(scope(Invoice.objects.select_related(
        "org_unit", "provider", "processing_owner", "created_by"), user), data).filter(
        Q(processing_owner=user) | Q(created_by=user) |
        Q(pk__in=pending_for(user).values("invoice_id")) |
        Q(batch_memberships__active=True, batch_memberships__batch_id__in=batches.values("pk"))
    ).distinct().order_by("due_date", "pk")
    services = apply_scope(scope(ServiceAccount.objects.select_related("org_unit", "provider"), user), data)
    providers = Provider.objects.filter(pk__in=services.values("provider_id")).order_by("name")
    return {"batches": batches, "invoices": invoices, "services": services.order_by("account_number", "pk"),
            "providers": providers}


def space_row(key, obj):
    if key == "invoices":
        row = row_for("invoice_anomalies", obj)
        row["kind"] = "Invoice"
        return row
    if key == "batches":
        return {"kind": "Batch", "title": f"{obj.reference} · {obj.name}", "url": obj.get_absolute_url(),
            "unit": str(obj.org_unit), "provider": str(obj.provider), "owner": str(obj.created_by),
            "status": {"preparation": "Preparation", "work": "In progress", "completed": "Completed",
                       "rejected": "Returned / rejected"}.get(obj.batch_folder, obj.batch_folder),
            "detail": "Batch lifecycle and audit evidence", "next_action": "Inspect batch"}
    if key == "services":
        return {"kind": "Service / account", "title": obj.account_number,
            "url": reverse("providers:account", args=[obj.pk]), "unit": str(obj.org_unit),
            "provider": str(obj.provider), "owner": obj.owner, "status": obj.get_status_display(),
            "detail": " · ".join(filter(None, [obj.service_type, obj.service_details, obj.purpose])),
            "next_action": "Inspect service account"}
    return {"kind": "Utility provider", "title": obj.name,
        "url": reverse("providers:visibility") + f"?provider={obj.pk}",
        "provider": obj.short_code, "status": "Active" if obj.is_active else "Inactive",
        "detail": obj.get_utility_type_display(), "next_action": "Open provider visibility"}


@login_required
def my_space(request):
    params = request.GET.copy()
    current_provider = getattr(request, "utility_provider", None)
    if "provider" in params and not params["provider"] and current_provider:
        params["clear_provider"] = "1"
        return redirect(space_url(params))
    if "provider" not in params and current_provider:
        params["provider"] = current_provider.pk
        return redirect(space_url(params))
    tab, folder = params.get("tab", "overview"), params.get("folder", "all")
    if tab not in TABS or folder not in SpaceFolder.Destination.values:
        raise Http404("Unknown personal view")
    if tab == "tasks":
        # One canonical task experience; retain old saved links without a duplicate listing.
        params["tab"], params["mode"] = "tasks", params.get("mode", "dashboard")
        params.pop("folder", None)
        return redirect(reverse("core:my_work") + "?" + params.urlencode())
    form = WorkFilter(params, user=request.user)
    context = {"tab": tab, "folder_filter": folder, "filter_form": form, "rows": [], "cards": [], "page": None,
        "tabs": [{"key": key, "label": label, "url": space_url(params, tab=key, folder="all")}
                 for key, label in TABS.items()],
        "folders": [folder_link(obj, request.user) for obj in SpaceFolder.objects.filter(
            owner=request.user).select_related("org_unit", "provider")],
        "scope_note": "Your owned and workflow-role-responsible batches and invoices; services/providers in your authorised units. Folder links do not change ownership or permissions."}
    if form.is_valid():
        tasks = personal_tasks(request.user, form.cleaned_data)
        queues = space_sources(request.user, form.cleaned_data, tasks)
        pending = queues["batches"].filter(members__active=True, members__invoice__status="in_approval").distinct()
        counts = {key: qs.count() for key, qs in queues.items()}
        task_count = sum(qs.count() for qs in tasks.values())
        context["cards"] = [
            {"label": "Utility providers", "count": counts["providers"], "url": space_url(params, tab="providers")},
            {"label": "Batches in progress", "count": queues["batches"].filter(batch_folder="work").count(),
             "url": space_url(params, tab="batches", folder="work")},
            {"label": "Batches pending approval", "count": pending.count(),
             "url": space_url(params, tab="batches", folder="pending")},
            {"label": "My invoices", "count": counts["invoices"], "url": space_url(params, tab="invoices")},
            {"label": "Services in my units", "count": counts["services"], "url": space_url(params, tab="services")},
            {"label": "Assigned work items", "count": task_count, "url": space_url(params, tab="tasks")},
        ]
        if tab == "batches" and folder not in {"all", "invoices", "services"}:
            queues["batches"] = pending if folder == "pending" else queues["batches"].filter(batch_folder=folder)
            context["scope_note"] += f" Current folder: {SpaceFolder.Destination(folder).label}."
        if tab in queues:
            page = paginate(request, queues[tab], 30)
            context.update(page=page, rows=[space_row(tab, obj) for obj in page.object_list])
        elif tab == "overview":
            context["rows"] = [space_row("batches", obj) for obj in queues["batches"][:5]]
            context["rows"] += [space_row("invoices", obj) for obj in queues["invoices"][:5]]
    return render(request, "core/my_space.html", context, status=400 if form.errors else 200)
