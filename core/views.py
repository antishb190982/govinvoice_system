from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.decorators.http import require_GET
from django.views.decorators.cache import never_cache
from django.http import JsonResponse

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
@require_GET
@never_cache
def activity(request):
    from operations.workspace import counts_for
    from core.attention import attention_summary
    notes = request.user.notifications
    attention = attention_summary(request.user)
    return JsonResponse({
        "unread_notifications": attention["unread_count"],
        "attention": attention,
        "pending_approvals": pending_for(request.user).filter(
            invoice__batch_memberships__active=True).values(
            "invoice__batch_memberships__batch_id").distinct().count(),
        "batch_folders": counts_for(request.user),
        "latest_notification_id": notes.values_list("pk", flat=True).first() or 0,
    })


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
    my_tasks = pending_for(user).filter(invoice__batch_memberships__active=True) \
        .select_related("invoice__provider", "invoice__org_unit").order_by("due_at")
    from operations.models import InvoiceBatch
    from django.db.models import Count, Min, Sum
    batch_ids = my_tasks.values_list("invoice__batch_memberships__batch_id", flat=True).distinct()
    my_approval_batches = []
    for batch in scope(InvoiceBatch.objects.filter(pk__in=batch_ids, cancelled_at__isnull=True),
                       user).select_related("provider", "org_unit"):
        tasks = my_tasks.filter(invoice__batch_memberships__batch=batch,
                                invoice__batch_memberships__active=True)
        summary = tasks.aggregate(due=Min("due_at"), count=Count("pk"))
        summary.update(batch=batch, invoice_count=batch.members.filter(active=True).count(),
            total=batch.members.filter(active=True).aggregate(total=Sum("original_amount"))["total"])
        my_approval_batches.append(summary)
    my_approval_batches.sort(key=lambda row: row["due"])
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
        "my_approval_batches": my_approval_batches[:8],
        "my_tasks_count": len(my_approval_batches),
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
    from core.notify import channel_readiness
    page = paginate(request, request.user.notifications.prefetch_related("deliveries"), 30)
    return render(request, "core/notifications.html", {
        "page": page, "channel_readiness": channel_readiness(),
    })


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


def favicon(request):
    from django.templatetags.static import static
    return redirect(static("favicon.svg"))

