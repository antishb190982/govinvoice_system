from django.conf import settings

from org.access import active_memberships
from org.models import Role


def app_context(request):
    user = getattr(request, "user", None)
    ctx = {"DEFAULT_CURRENCY": settings.DEFAULT_CURRENCY, "ENVIRONMENT": settings.ENVIRONMENT,
           "IDLE_TIMEOUT_MINUTES": settings.SESSION_IDLE_TIMEOUT_MINUTES,
           "DUE_SOON_DAYS": settings.DUE_SOON_DAYS}
    if user is None or not user.is_authenticated or not request.session.get("otp_verified"):
        return ctx
    roles = set(Role.values) if user.is_superuser else set(active_memberships(user).values_list("role", flat=True))
    from workflow.engine import pending_for

    ctx.update({
        "my_roles": roles,
        "unread_notifications": user.notifications.filter(read_at__isnull=True).count(),
        "pending_approvals": pending_for(user).count() if roles & {Role.VERIFIER, Role.APPROVER, Role.UNIT_ADMIN} else 0,
        "can": {
            "capture": bool(roles & {Role.CLERK, Role.UNIT_ADMIN}),
            "approve": bool(roles & {Role.VERIFIER, Role.APPROVER, Role.UNIT_ADMIN}),
            "settle": bool(roles & {Role.FINANCE, Role.AUTHORISER}),
            "admin": bool(roles & {Role.UNIT_ADMIN}),
            "audit": user.is_superuser or Role.AUDITOR in roles,
            "sysadmin": user.is_superuser,
        },
    })
    return ctx
