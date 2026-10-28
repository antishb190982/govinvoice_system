from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from analytics import queries
from core.models import AuditEvent, Notification
from core.utils import csv_response, paginate
from invoices.models import Invoice
from org.access import scope, units_for, user_has_any_role
from org.models import Role
from settlements.models import SettlementInstruction
from workflow.engine import pending_for
from workflow.models import ApprovalTask


@login_required
def dashboard(request):
    user = request.user
    today = timezone.localdate()
    soon = today + timedelta(days=settings.DUE_SOON_DAYS)
    cur = settings.DEFAULT_CURRENCY
    invoices = scope(Invoice.objects.filter(currency=cur), user)
    unpaid = invoices.filter(status__in=Invoice.UNPAID)

    def total(qs):
        return qs.aggregate(t=Sum("amount_total"), n=Count("id"))

    at_risk = unpaid.filter(
        Q(status=Invoice.Status.APPROVED, due_date__lte=soon) |
        Q(status__in=[Invoice.Status.IN_APPROVAL, Invoice.Status.RETURNED], must_approve_by__lt=timezone.now())
    )
    my_tasks = pending_for(user).select_related("invoice__provider", "invoice__org_unit").order_by("due_at")
    tasks_scope = scope(ApprovalTask.objects.all(), user, prefix="invoice__org_unit__")
    decided_30 = tasks_scope.filter(decided_at__gte=timezone.now() - timedelta(days=30))
    on_time = decided_30.filter(breached=False).count()
    ctx = {
        "kpi": {
            "outstanding": total(unpaid),
            "due_soon": total(unpaid.filter(due_date__gte=today, due_date__lte=soon)),
            "overdue": total(unpaid.filter(due_date__lt=today)),
            "at_risk": total(at_risk),
            "in_approval": total(invoices.filter(status=Invoice.Status.IN_APPROVAL)),
            "awaiting_settlement": total(invoices.filter(status=Invoice.Status.APPROVED)),
            "sla_rate": round(100 * on_time / decided_30.count()) if decided_30.count() else None,
            "sla_breaches_open": tasks_scope.filter(status=ApprovalTask.Status.PENDING, due_at__lt=timezone.now()).count(),
            "flagged": unpaid.exclude(flags=[]).count(),
        },
        "currency": cur,
        "soon": soon,
        "my_tasks": my_tasks[:8],
        "my_tasks_count": my_tasks.count(),
        "upcoming": unpaid.filter(due_date__lte=soon).select_related("provider", "org_unit").order_by("due_date")[:10],
        "settlements_pending": scope(SettlementInstruction.objects.filter(
            status__in=[SettlementInstruction.Status.DRAFT, SettlementInstruction.Status.PENDING_AUTHORISATION]),
            user).order_by("pay_by")[:5],
        "chart_monthly": queries.monthly_spend(invoices, months=12),
        "chart_due": queries.due_profile(unpaid, weeks=6),
        "notifications": user.notifications.filter(read_at__isnull=True)[:5],
    }
    return render(request, "core/dashboard.html", ctx)


@login_required
def notifications(request):
    page = paginate(request, request.user.notifications.all(), 30)
    return render(request, "core/notifications.html", {"page": page})


@login_required
@require_POST
def notification_read(request, pk=None):
    qs = request.user.notifications.filter(read_at__isnull=True)
    if pk:
        note = get_object_or_404(Notification, pk=pk, user=request.user)
        qs = qs.filter(pk=note.pk)
        target = note.link
    else:
        target = ""
    qs.update(read_at=timezone.now())
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}):
        return redirect(target)
    return redirect("core:notifications")


def _audit_scope(user):
    if user.is_superuser:
        return AuditEvent.objects.all()
    if not user_has_any_role(user, [Role.AUDITOR]):
        raise PermissionDenied
    unit_ids = list(units_for(user, [Role.AUDITOR]).values_list("pk", flat=True))
    return AuditEvent.objects.filter(org_unit_id__in=unit_ids)


@login_required
def audit_log(request):
    events = _audit_scope(request.user).select_related("actor")
    q = request.GET.get("q", "").strip()
    action = request.GET.get("action", "").strip()
    if q:
        events = events.filter(Q(actor_label__icontains=q) | Q(object_id=q) | Q(data__icontains=q))
    if action:
        events = events.filter(action__startswith=action)
    if request.GET.get("export") == "csv":
        return csv_response("audit-log.csv", ["sequence", "time", "actor", "action", "object", "id", "ip", "data", "hash"],
                            ([e.sequence, e.occurred_at.isoformat(), e.actor_label, e.action, e.object_type,
                              e.object_id, e.ip_address, e.data, e.entry_hash] for e in events[:50000]))
    chain = None
    if request.GET.get("verify"):
        ok, bad, count = AuditEvent.verify_chain()
        chain = {"ok": ok, "bad": bad, "count": count}
        AuditEvent.record("audit.chain_verified", data={"ok": ok, "count": count})
    actions = sorted({a.split(".")[0] for a in AuditEvent.objects.values_list("action", flat=True).distinct()})
    return render(request, "core/audit_log.html", {"page": paginate(request, events, 50), "q": q, "action": action,
                                                   "actions": actions, "chain": chain})


def handler403(request, exception=None):
    return render(request, "403.html", status=403)


def handler404(request, exception=None):
    return render(request, "404.html", status=404)

