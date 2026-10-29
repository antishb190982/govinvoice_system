from django.conf import settings

from org.access import active_memberships
from org.models import Role


def app_context(request):
    user = getattr(request, "user", None)
    ctx = {"DEFAULT_CURRENCY": settings.DEFAULT_CURRENCY, "ENVIRONMENT": settings.ENVIRONMENT,
           "IDLE_TIMEOUT_MINUTES": settings.SESSION_IDLE_TIMEOUT_MINUTES,
           "DUE_SOON_DAYS": settings.DUE_SOON_DAYS, "BRAND_NAME": settings.BRAND_NAME,
           "BRAND_TAGLINE": settings.BRAND_TAGLINE}
    if user is None or not user.is_authenticated or not request.session.get("otp_verified"):
        return ctx
    roles = set(Role.values) if user.is_superuser else set(active_memberships(user).values_list("role", flat=True))
    from workflow.engine import pending_for
    from operations.workspace import counts_for
    from analytics.sections import section_navigation

    ctx.update(section_navigation(request))
    from core.provider_scope import available_providers
    ctx.update({
        "provider_options": available_providers(user),
        "utility_provider": getattr(request, "utility_provider", None),
        "provider_filter_params": [(k, v) for k, values in request.GET.lists()
            if k not in {"provider", "utility_provider", "page"} for v in values],
    })
    ctx.update({
        "nav_units": list(active_memberships(user).order_by("org_unit__name").values_list("org_unit__name", flat=True).distinct()[:3]),
        "my_roles": roles,
        "unread_notifications": user.notifications.filter(read_at__isnull=True).count(),
        "batch_folder_counts": getattr(request, "batch_folder_counts", None) or counts_for(user),
        "pending_approvals": pending_for(user).filter(invoice__batch_memberships__active=True).values(
            "invoice__batch_memberships__batch_id").distinct().count() if roles & {Role.VERIFIER, Role.APPROVER, Role.UNIT_ADMIN} else 0,
        "can": {
            "capture": bool(roles & {Role.CLERK, Role.UNIT_ADMIN}),
            "approve": bool(roles & {Role.VERIFIER, Role.APPROVER, Role.UNIT_ADMIN}),
            "settle": bool(roles & {Role.FINANCE, Role.AUTHORISER}),
            "admin": bool(roles & {Role.UNIT_ADMIN}),
            "audit": user.is_superuser or Role.AUDITOR in roles,
            "sysadmin": user.is_superuser,
            "dispute": bool(roles & {Role.CLERK, Role.UNIT_ADMIN, Role.FINANCE, Role.VERIFIER, Role.APPROVER, Role.AUTHORISER}),
            "bank": bool(roles & {Role.FINANCE, Role.AUTHORISER, Role.UNIT_ADMIN, Role.AUDITOR}),
        },
    })
    from core.my_work import dashboard_overdue
    ctx["dashboard_overdue"] = dashboard_overdue(request)
    from core.navigation import navigation_context
    ctx.update(navigation_context(request))
    return ctx
