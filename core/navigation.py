from urllib.parse import urlencode

from django.urls import reverse


def navigation_context(request):
    match = request.resolver_match
    if not match:
        return {}
    params = {key: request.GET[key] for key in ["org_unit", "provider"] if request.GET.get(key)}
    provider = getattr(request, "utility_provider", None)
    if provider:
        params["provider"] = str(provider.pk)

    def url(route, **extra):
        data = {**params, **extra}
        return reverse(route) + ("?" + urlencode(data) if data else "")

    context = {}
    name = match.url_name or ""
    if match.app_name == "providers" and "account" not in name:
        context["provider_workspace_tabs"] = [
            {"label": "Overview dashboard", "url": url("providers:visibility"), "active": name == "visibility"},
            {"label": "Provider catalogue", "url": url("providers:list"), "active": name != "visibility"},
        ]
    if match.app_name == "core" and (name in {"my_space", "my_work"} or
            name.startswith("space_folder") or name.startswith("work_schedule")):
        work = name == "my_work" or name.startswith("work_schedule")
        current = "work" if work else "folders" if name.startswith("space_folder") else request.GET.get("tab", "overview")
        labels = [("overview", "Overview"), ("providers", "Utility providers"), ("batches", "Batches"),
                  ("invoices", "Invoices"), ("services", "Services"), ("work", "Work"), ("folders", "My folders")]
        context["personal_space_tabs"] = [{
            "key": key, "label": label, "active": key == current,
            "url": url("core:my_work", tab="tasks", mode="dashboard") if key == "work" else
                   url("core:my_space", tab=key),
        } for key, label in labels]
    if match.view_name in {"home", "core:dashboard", "core:control_centre"}:
        context["operations_tabs"] = [
            {"label": "Overview dashboard", "url": url("core:dashboard"), "active": name != "control_centre"},
            {"label": "Action queues", "url": url("core:control_centre"), "active": name == "control_centre"},
        ]
    return context
