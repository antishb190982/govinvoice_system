"""Presentation-only provider context layered on top of ministry access."""
from contextvars import ContextVar
from providers.models import Provider, ServiceAccount

_provider = ContextVar("govbills_provider", default=None)


def available_providers(user):
    from org.access import unit_filter
    if user.is_superuser:
        return Provider.objects.all()
    return Provider.objects.filter(pk__in=ServiceAccount.objects.filter(
        unit_filter(user)).values("provider_id")).distinct()


def filter_provider(qs):
    provider = _provider.get()
    if not provider:
        return qs
    names = {f.name for f in qs.model._meta.fields}
    if qs.model is Provider:
        return qs.filter(pk=provider)
    if "provider" in names:
        return qs.filter(provider_id=provider)
    if "invoice" in names:
        return qs.filter(invoice__provider_id=provider)
    if qs.model._meta.label_lower == "reconciliation.statementrow":
        return qs.filter(statement__provider_id=provider)
    if qs.model._meta.label_lower == "imports.importrow":
        return qs.filter(batch__provider_id=provider)
    return qs


def activate(request):
    selected = request.session.get("utility_provider")
    provider_scoped = (request.GET.get("provider", "").isdigit()
                       and not request.path.rstrip("/").endswith("/providers/accounts/new"))
    explicit = "utility_provider" in request.GET or provider_scoped
    if "utility_provider" in request.GET:
        selected = request.GET["utility_provider"]
    elif provider_scoped:
        selected = request.GET["provider"]
    elif request.GET.get("clear_provider") == "1":
        selected = None
    user = getattr(request, "user", None)
    request.utility_provider = None
    if user and user.is_authenticated:
        if selected:
            if not str(selected).isdigit():
                raise ValueError("Choose a valid utility provider.")
            request.utility_provider = available_providers(user).filter(pk=int(selected)).first()
            if not request.utility_provider:
                if explicit:
                    raise ValueError("This provider has no allocated accounts in your authorised ministries.")
                request.session.pop("utility_provider", None)
        if request.utility_provider:
            request.session["utility_provider"] = request.utility_provider.pk
        else:
            request.session.pop("utility_provider", None)
    return _provider.set(request.utility_provider.pk if request.utility_provider else None)


def reset(token):
    _provider.reset(token)
