from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.utils import paginate
from invoices.models import Flag
from org.access import can_view, scope
from org.models import Role
from signatures.services import active_identity
from workflow import engine
from workflow.models import ApprovalPolicy, ApprovalTask


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=engine.Decision.CHOICES, widget=forms.RadioSelect)
    comment = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), max_length=2000,
                              help_text="Required when returning or disputing, or when approving a flagged invoice.")
    pin = forms.CharField(required=False, label="Signing PIN",
                          widget=forms.PasswordInput(attrs={"autocomplete": "off"}))


@login_required
def inbox(request):
    tasks = engine.pending_for(request.user).select_related(
        "invoice__provider", "invoice__org_unit", "invoice__service_account").order_by("due_at")
    if request.GET.get("attention") == "overdue":
        tasks = tasks.filter(due_at__lt=timezone.now())
    elif request.GET.get("attention") == "expedited":
        tasks = tasks.filter(expedited=True)
    if request.GET.get("role") in Role.values:
        tasks = tasks.filter(role=request.GET["role"])
    return render(request, "workflow/inbox.html", {"tasks": tasks, "now": timezone.now()})


@login_required
def sla_board(request):
    tasks = scope(ApprovalTask.objects.filter(status=ApprovalTask.Status.PENDING), request.user,
                  prefix="invoice__org_unit__").select_related("invoice__provider", "invoice__org_unit").order_by("due_at")
    recent = scope(ApprovalTask.objects.filter(decided_at__isnull=False), request.user, prefix="invoice__org_unit__") \
        .select_related("invoice", "decided_by").order_by("-decided_at")
    return render(request, "workflow/sla_board.html", {"tasks": tasks, "page": paginate(request, recent, 20),
                                                       "now": timezone.now()})


@login_required
def task_detail(request, pk):
    task = get_object_or_404(ApprovalTask.objects.select_related(
        "invoice__provider", "invoice__org_unit", "invoice__service_account", "invoice__created_by"), pk=pk)
    invoice = task.invoice
    if not can_view(request.user, invoice.org_unit):
        raise Http404
    block_reason = None
    try:
        engine.check_can_decide(task, request.user)
    except (PermissionDenied, ValidationError) as exc:
        block_reason = " ".join(getattr(exc, "messages", [str(exc)]))
    form = DecisionForm(request.POST or None)
    if request.method == "POST" and not block_reason and form.is_valid():
        d = form.cleaned_data
        try:
            engine.decide(task, request.user, d["decision"], d["comment"], pin=d["pin"])
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, " ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, f"Decision recorded for {invoice.reference}.")
            # Keep the success message and resulting status on the same
            # invoice; do not silently switch to another ministry's task.
            return redirect(invoice)
    previous = invoice.approval_tasks.filter(round=task.round, order__lt=task.order).select_related("decided_by")
    return render(request, "workflow/task.html", {
        "task": task, "invoice": invoice, "form": form, "block_reason": block_reason, "previous": previous,
        "flags": [(f, Flag.LABELS.get(f, f), f in Flag.SEVERE) for f in invoice.flags],
        "has_severe": bool(set(invoice.flags) & Flag.SEVERE),
        "identity": active_identity(request.user), "now": timezone.now(),
    })


@login_required
def policies(request):
    from org.access import has_role, user_has_any_role
    from org.models import Role
    from workflow.configuration import policy_nodes, readable_policies

    items = list(readable_policies(request.user))
    for item in items:
        item.can_edit = has_role(request.user, item.org_unit, [Role.UNIT_ADMIN])
        item.nodes = policy_nodes(item)
    return render(request, "workflow/policies.html", {
        "policies": items, "can_configure": user_has_any_role(request.user, [Role.UNIT_ADMIN]),
    })
