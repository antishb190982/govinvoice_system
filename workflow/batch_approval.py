"""One atomic batch decision, with the existing signed invoice evidence retained."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from core.models import AuditEvent
from invoices.models import Invoice
from operations.models import InvoiceBatch
from org.access import can_view
from workflow import engine
from workflow.models import ApprovalTask


def stage_tasks(batch, user):
    invoices = list(Invoice.objects.filter(batch_memberships__batch=batch,
        batch_memberships__active=True).order_by("pk"))
    if batch.cancelled_at or not invoices or any(i.status != Invoice.Status.IN_APPROVAL for i in invoices):
        raise ValidationError("Every invoice must be in approval at the same stage. Ask the preparer to resolve this batch.")
    tasks = list(ApprovalTask.objects.filter(invoice__in=invoices,
        status=ApprovalTask.Status.PENDING).select_related("invoice").order_by("invoice_id", "pk"))
    if len(tasks) != len(invoices) or len({t.invoice_id for t in tasks}) != len(invoices):
        raise ValidationError("Not all invoices are ready for a batch decision.")
    if len({(t.order, t.role, t.name, t.requires_signature) for t in tasks}) != 1:
        raise ValidationError("Invoice workflow stages differ. Resolve the batch before deciding it.")
    for task in tasks:
        engine.check_can_decide(task, user)
    return tasks


@transaction.atomic
def decide_batch(batch, user, decision, comment, pin, expected_ids):
    batch = InvoiceBatch.objects.select_for_update().get(pk=batch.pk)
    if not can_view(user, batch.org_unit):
        raise PermissionDenied
    # Match lock order used by batch submission. Re-evaluate stages under locks.
    list(Invoice.objects.select_for_update().filter(batch_memberships__batch=batch,
        batch_memberships__active=True).order_by("pk"))
    list(ApprovalTask.objects.select_for_update().filter(invoice__batch_memberships__batch=batch,
        status=ApprovalTask.Status.PENDING).order_by("pk"))
    tasks = stage_tasks(batch, user)
    if set(expected_ids) != {t.pk for t in tasks}:
        raise ValidationError("This batch changed since you opened it. Reload before deciding.")
    if decision not in (engine.Decision.APPROVE, engine.Decision.RETURN, engine.Decision.DISPUTE):
        raise ValidationError("Choose a valid batch decision.")
    from invoices.models import Flag
    from operations.services import require_clear
    from reconciliation.services import require_reconciled
    if decision != engine.Decision.APPROVE and not comment.strip():
        raise ValidationError("Give a reason when returning or disputing a batch.")
    for task in tasks:
        invoice = task.invoice
        if decision == engine.Decision.APPROVE:
            require_clear(invoice)
            require_reconciled(invoice)
            if set(invoice.flags) & Flag.SEVERE and not comment.strip():
                raise ValidationError("Explain why you approve every invoice with an integrity warning.")
    if decision == engine.Decision.APPROVE and any(task.requires_signature for task in tasks):
        from signatures.services import check_pin
        check_pin(user, pin)
    for task in tasks:
        engine.decide(task, user, decision, comment, pin=pin)
    AuditEvent.record(f"batch.approval_{decision}", obj=batch, actor=user, org_unit_id=batch.org_unit_id,
        data={"tasks": [t.pk for t in tasks], "invoices": [t.invoice.reference for t in tasks],
              "comment": comment, "provider": batch.provider_id})
    return batch


@login_required
def review(request, pk):
    from workflow.views import DecisionForm
    batch = get_object_or_404(InvoiceBatch.objects.select_related("provider", "org_unit", "created_by"), pk=pk)
    if not can_view(request.user, batch.org_unit):
        raise PermissionDenied
    error, tasks = None, []
    try:
        tasks = stage_tasks(batch, request.user)
    except (ValidationError, PermissionDenied) as exc:
        error = " ".join(getattr(exc, "messages", [str(exc)]))
    form = DecisionForm(request.POST or None)
    if request.method == "POST" and not error and form.is_valid():
        try:
            ids = [int(i) for i in request.POST.getlist("tasks")]
            d = form.cleaned_data
            decide_batch(batch, request.user, d["decision"], d["comment"], d["pin"], ids)
        except (ValidationError, PermissionDenied, ValueError) as exc:
            form.add_error(None, " ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, f"Batch decision recorded for every invoice in {batch.reference}.")
            return redirect(batch)
    return render(request, "workflow/batch_review.html", {"batch": batch, "tasks": tasks,
        "members": batch.members.filter(active=True).select_related("invoice__service_account"),
        "block_reason": error, "form": form})
