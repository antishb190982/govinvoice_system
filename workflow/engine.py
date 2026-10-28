"""
Approval engine.

Rules enforced here (not just in the UI):
  * an invoice follows the most specific active policy for its unit, utility and amount;
  * whoever recorded or submitted an invoice may not decide any of its steps;
  * nobody decides more than one step of the same round;
  * a decider must hold the step's role on the invoice's unit or an ancestor
    (or the escalation role, once a step has breached its SLA);
  * signature steps need the decider's signing PIN and produce a signature over
    the invoice snapshot, the attached document hash and the decision.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Notification
from core.notify import notify
from core.worktime import add_working_hours
from invoices.models import Flag, Invoice
from org.access import has_role, users_with_role
from org.models import Role
from workflow.models import ApprovalPolicy, ApprovalTask

EXPEDITED_HOURS = 4
REMIND_AT_REMAINING = 0.25


class Decision:
    APPROVE = "approve"
    RETURN = "return"
    DISPUTE = "dispute"
    CHOICES = [(APPROVE, "Approve"), (RETURN, "Return for correction"), (DISPUTE, "Dispute with provider")]


def select_policy(invoice):
    ancestor_ids = invoice.org_unit.ancestor_ids
    candidates = ApprovalPolicy.objects.filter(
        is_active=True, org_unit_id__in=ancestor_ids, min_amount__lte=invoice.amount_total,
    ).filter(Q(max_amount__isnull=True) | Q(max_amount__gte=invoice.amount_total)) \
        .filter(Q(utility_type="") | Q(utility_type=invoice.utility_type)).select_related("org_unit")

    def specificity(policy):
        return (-policy.org_unit.depth, 0 if policy.utility_type else 1, policy.priority, policy.pk)

    ranked = sorted(candidates, key=specificity)
    return ranked[0] if ranked else None


def _activate(task, now=None):
    now = now or timezone.now()
    invoice = task.invoice
    sla_due = add_working_hours(now, task.sla_hours)
    if invoice.must_approve_by and invoice.must_approve_by > now:
        task.due_at = min(sla_due, invoice.must_approve_by)
        task.expedited = task.due_at < sla_due
    else:
        task.due_at = add_working_hours(now, min(task.sla_hours, EXPEDITED_HOURS))
        task.expedited = True
    task.activated_at = now
    task.status = ApprovalTask.Status.PENDING
    task.save()
    approvers = users_with_role(invoice.org_unit, [task.role]).exclude(pk=invoice.created_by_id)
    notify(approvers, f"Approval needed: {invoice.reference}",
           f"{invoice.provider.name} {invoice.invoice_number} – {invoice.currency} {invoice.amount_total:,} "
           f"due {invoice.due_date:%d %b %Y}. Step: {task.name}. Decide by {timezone.localtime(task.due_at):%d %b %H:%M}.",
           link=reverse("workflow:task", args=[task.pk]),
           level=Notification.Level.WARNING if task.expedited else Notification.Level.INFO,
           dedupe_key=f"task-{task.pk}-activated")


@transaction.atomic
def submit(invoice, user):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status not in (Invoice.Status.DRAFT, Invoice.Status.RETURNED, Invoice.Status.DISPUTED):
        raise ValidationError("Only draft, returned or disputed invoices can be submitted.")
    if not has_role(user, invoice.org_unit, [Role.CLERK, Role.UNIT_ADMIN]):
        raise PermissionDenied("You cannot submit invoices for this unit.")
    if not invoice.document:
        raise ValidationError("Attach the provider's invoice document before submitting.")
    policy = select_policy(invoice)
    if policy is None:
        raise ValidationError("No approval policy covers this unit, utility and amount. Ask an administrator.")
    steps = [s for s in policy.steps.order_by("order") if invoice.amount_total >= s.min_amount]
    if not steps:
        raise ValidationError(f"Policy “{policy.name}” has no steps for this amount.")

    current_round = (invoice.approval_tasks.aggregate(r=Max("round"))["r"] or 0) + 1
    invoice.approval_tasks.filter(status__in=[ApprovalTask.Status.WAITING, ApprovalTask.Status.PENDING]) \
        .update(status=ApprovalTask.Status.CANCELLED)
    tasks = [ApprovalTask.objects.create(
        invoice=invoice, round=current_round, order=i, step=s, name=s.name, role=s.role,
        escalation_role=s.escalation_role, sla_hours=s.sla_hours, requires_signature=s.requires_signature,
    ) for i, s in enumerate(steps, start=1)]

    invoice.status = Invoice.Status.IN_APPROVAL
    invoice.submitted_at = timezone.now()
    invoice.save(update_fields=["status", "submitted_at", "updated_at"])
    _activate(tasks[0])
    AuditEvent.record("invoice.submitted", obj=invoice, actor=user,
                      data={"policy": policy.name, "round": current_round, "steps": [t.name for t in tasks]})
    return tasks


def check_can_decide(task, user):
    invoice = task.invoice
    if task.status != ApprovalTask.Status.PENDING:
        raise ValidationError("This step is not awaiting a decision.")
    if user.pk == invoice.created_by_id:
        raise PermissionDenied("Segregation of duties: you recorded this invoice.")
    submitters = AuditEvent.objects.filter(action="invoice.submitted", object_type="invoices.invoice",
                                           object_id=str(invoice.pk)).values_list("actor_id", flat=True)
    if user.pk in set(submitters):
        raise PermissionDenied("Segregation of duties: you submitted this invoice.")
    if ApprovalTask.objects.filter(invoice=invoice, round=task.round, decided_by=user).exists():
        raise PermissionDenied("Segregation of duties: you already decided a step of this approval.")
    if not has_role(user, invoice.org_unit, task.eligible_roles):
        raise PermissionDenied("You do not hold the role required for this step.")


def decision_payload(task, decision, comment, decided_at):
    return {
        "type": "invoice-approval-decision",
        "invoice": task.invoice.snapshot(),
        "task": {"id": task.pk, "round": task.round, "order": task.order, "name": task.name},
        "decision": decision,
        "comment": comment,
        "decided_at": decided_at.isoformat(),
    }


def decide(task, user, decision, comment="", pin=None):
    if task.requires_signature and decision == Decision.APPROVE:
        from signatures.services import check_pin

        check_can_decide(task, user)
        check_pin(user, pin)
    return _decide(task, user, decision, comment, pin)


@transaction.atomic
def _decide(task, user, decision, comment, pin):
    task = ApprovalTask.objects.select_for_update().select_related("invoice__org_unit", "invoice__provider",
                                                                   "invoice__service_account").get(pk=task.pk)
    invoice = Invoice.objects.select_for_update().get(pk=task.invoice_id)
    task.invoice = invoice
    check_can_decide(task, user)
    if decision not in (Decision.APPROVE, Decision.RETURN, Decision.DISPUTE):
        raise ValidationError("Unknown decision.")
    comment = (comment or "").strip()
    if decision != Decision.APPROVE and not comment:
        raise ValidationError("Give a reason when returning or disputing an invoice.")
    severe = set(invoice.flags) & Flag.SEVERE
    if decision == Decision.APPROVE and severe and not comment:
        raise ValidationError("This invoice has integrity warnings; explain why you approve it anyway.")

    now = timezone.now().replace(microsecond=0)
    signature = None
    if task.requires_signature and decision == Decision.APPROVE:
        from signatures.services import sign_payload

        signature = sign_payload(user, pin, decision_payload(task, decision, comment, now),
                                 purpose=f"approval:{task.pk}")

    task.decided_at = now
    task.decided_by = user
    task.comment = comment
    task.breached = task.breached or bool(task.due_at and now > task.due_at)
    task.status = {Decision.APPROVE: ApprovalTask.Status.APPROVED, Decision.RETURN: ApprovalTask.Status.RETURNED,
                   Decision.DISPUTE: ApprovalTask.Status.DISPUTED}[decision]
    task.save()
    if signature:
        signature.approval_task = task
        signature.save(update_fields=["approval_task"])

    AuditEvent.record(f"approval.{decision}", obj=invoice, actor=user, data={
        "task": task.pk, "step": task.name, "round": task.round, "comment": comment,
        "late": task.decided_late, "signature": signature.pk if signature else None,
    })

    if decision == Decision.APPROVE:
        nxt = invoice.approval_tasks.filter(round=task.round, status=ApprovalTask.Status.WAITING).order_by("order").first()
        if nxt:
            _activate(nxt, now)
        else:
            invoice.status = Invoice.Status.APPROVED
            invoice.approved_at = now
            invoice.save(update_fields=["status", "approved_at", "updated_at"])
            AuditEvent.record("invoice.approved", obj=invoice, actor=user)
            notify(users_with_role(invoice.org_unit, [Role.FINANCE]),
                   f"Ready for settlement: {invoice.reference}",
                   f"{invoice.provider.name} {invoice.currency} {invoice.amount_total:,}, due {invoice.due_date:%d %b %Y}.",
                   link=invoice.get_absolute_url(), dedupe_key=f"invoice-{invoice.pk}-approved")
    else:
        invoice.approval_tasks.filter(round=task.round, status=ApprovalTask.Status.WAITING) \
            .update(status=ApprovalTask.Status.CANCELLED)
        invoice.status = Invoice.Status.RETURNED if decision == Decision.RETURN else Invoice.Status.DISPUTED
        invoice.save(update_fields=["status", "updated_at"])
        notify([invoice.created_by], f"{invoice.reference} {invoice.get_status_display().lower()}",
               f"{user.get_full_name()}: {comment}", link=invoice.get_absolute_url(),
               level=Notification.Level.WARNING, dedupe_key=f"task-{task.pk}-{decision}")
    return task


def pending_for(user):
    """Pending tasks the user may decide right now."""
    from org.access import active_memberships

    if user.is_superuser:
        return ApprovalTask.objects.filter(status=ApprovalTask.Status.PENDING)
    q = Q(pk__in=[])
    for m in active_memberships(user):
        unit_q = Q(invoice__org_unit__path__startswith=m.org_unit.path)
        q |= unit_q & (Q(role=m.role) | Q(escalation_role=m.role, escalated_at__isnull=False))
    return ApprovalTask.objects.filter(status=ApprovalTask.Status.PENDING).filter(q) \
        .exclude(invoice__created_by=user).exclude(invoice__approval_tasks__decided_by=user).distinct()


def sweep_sla(now=None):
    """Reminders before an SLA lapses; breach marking and escalation after."""
    now = now or timezone.now()
    reminded = escalated = 0
    for task in ApprovalTask.objects.filter(status=ApprovalTask.Status.PENDING).select_related(
            "invoice__org_unit", "invoice__provider"):
        invoice = task.invoice
        link = reverse("workflow:task", args=[task.pk])
        if task.due_at and now > task.due_at and not task.escalated_at:
            task.breached = True
            task.escalated_at = now
            task.save(update_fields=["breached", "escalated_at"])
            parent = invoice.org_unit.parent or invoice.org_unit
            recipients = (users_with_role(invoice.org_unit, [task.role]) |
                          users_with_role(parent, [task.escalation_role])).distinct()
            notify(recipients, f"SLA breached: {invoice.reference} – {task.name}",
                   f"The step was due {timezone.localtime(task.due_at):%d %b %H:%M}. Invoice due "
                   f"{invoice.due_date:%d %b %Y}. Escalated to {task.get_escalation_role_display()}.",
                   link=link, level=Notification.Level.CRITICAL, dedupe_key=f"task-{task.pk}-breach")
            AuditEvent.record("approval.sla_breached", obj=invoice, data={"task": task.pk, "step": task.name})
            escalated += 1
        elif not task.reminded_at and (task.remaining_fraction() or 1) <= REMIND_AT_REMAINING:
            task.reminded_at = now
            task.save(update_fields=["reminded_at"])
            notify(users_with_role(invoice.org_unit, [task.role]).exclude(pk=invoice.created_by_id),
                   f"Reminder: {invoice.reference} awaits {task.name}",
                   f"Decide by {timezone.localtime(task.due_at):%d %b %H:%M} to keep the SLA.",
                   link=link, level=Notification.Level.WARNING, dedupe_key=f"task-{task.pk}-remind")
            reminded += 1
    return reminded, escalated


def sla_projection(invoice):
    """Projected approval completion if every remaining step takes its full SLA."""
    tasks = list(invoice.approval_tasks.filter(status__in=[ApprovalTask.Status.PENDING, ApprovalTask.Status.WAITING])
                 .order_by("order"))
    if not tasks:
        return None
    moment = timezone.now()
    for t in tasks:
        moment = t.due_at if t.status == ApprovalTask.Status.PENDING and t.due_at else add_working_hours(moment, t.sla_hours)
    return moment


def default_policy_steps():
    """Steps the seed/setup command installs for a new government-wide policy."""
    return [
        {"name": "Verification of consumption & account", "role": Role.VERIFIER, "sla_hours": 8},
        {"name": "Budget holder approval", "role": Role.APPROVER, "sla_hours": 16, "requires_signature": True},
        {"name": "Senior approval (high value)", "role": Role.APPROVER, "sla_hours": 16,
         "requires_signature": True, "min_amount": 5000, "escalation_role": Role.UNIT_ADMIN},
    ]

