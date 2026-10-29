from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.files import CONTENT_TYPES
from core.models import AuditEvent
from core.utils import csv_response, file_response, paginate, require_any_role
from invoices.forms import CommentForm, InvoiceFilterForm, InvoiceForm, ReasonForm
from invoices.models import Flag, Invoice, InvoiceComment
from invoices.services import save_invoice
from org.access import can_view, has_role, scope
from org.models import Role
from workflow import engine
from workflow.models import ApprovalTask


def _get_invoice(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("provider", "service_account", "org_unit", "created_by"),
                                pk=pk)
    if not can_view(request.user, invoice.org_unit):
        raise Http404
    return invoice


def filtered_invoices(request):
    form = InvoiceFilterForm(request.GET or None, user=request.user)
    qs = scope(Invoice.objects.select_related("provider", "org_unit", "service_account"), request.user)
    from invoices.intake_dates import with_processing_priority

    qs = with_processing_priority(qs)
    if form.is_valid():
        d = form.cleaned_data
        if d["q"]:
            qs = qs.filter(Q(reference__icontains=d["q"]) | Q(invoice_number__icontains=d["q"]) |
                           Q(service_account__account_number__icontains=d["q"]) | Q(provider__name__icontains=d["q"]))
        if d["status"] == "open":
            qs = qs.filter(status__in=Invoice.UNPAID)
        elif d["status"] == "billed":
            qs = qs.exclude(status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED])
        elif d["status"]:
            qs = qs.filter(status=d["status"])
        if d["provider"]:
            qs = qs.filter(provider_id=d["provider"])
        if d["currency"]:
            qs = qs.filter(currency=d["currency"])
        if d["utility"]:
            qs = qs.filter(utility_type=d["utility"])
        if d["org_unit"]:
            qs = qs.filter(org_unit__path__startswith=d["org_unit"].path)
        if d["due_from"]:
            qs = qs.filter(due_date__gte=d["due_from"])
        if d["due_to"]:
            qs = qs.filter(due_date__lte=d["due_to"])
        if d["flagged"]:
            qs = qs.exclude(flags=[])
        if d["priority"] == "urgent":
            qs = qs.filter(processing_rank__gte=3)
        elif d["priority"] == "high":
            qs = qs.filter(processing_rank=2)
        elif d["priority"] == "normal":
            qs = qs.filter(processing_rank=1)
    return form, qs


@login_required
def invoice_list(request):
    form, qs = filtered_invoices(request)
    order = request.GET.get("sort", "priority")
    if order == "priority":
        qs = qs.order_by("-processing_rank", "due_date", "id")
    elif order.lstrip("-") in {"due_date", "received_date", "amount_total", "issue_date", "reference", "status"}:
        qs = qs.order_by(order, "id")
    if request.GET.get("export") == "csv":
        AuditEvent.record("invoice.exported", data={"filters": request.GET.dict()})
        return csv_response("invoices.csv", [
            "Reference", "Provider", "Utility", "Account", "Unit", "Invoice no", "Issue date", "Period start",
            "Period end", "Due date", "Currency", "Net", "Tax", "Total", "Consumption", "Unit", "Status", "Flags",
            "Paid on", "Payment ref"],
            ([i.reference, i.provider.name, i.get_utility_type_display(), i.service_account.account_number,
              i.org_unit.code, i.invoice_number, i.issue_date, i.period_start, i.period_end, i.due_date, i.currency,
              i.amount_net, i.amount_tax, i.amount_total, i.consumption, i.consumption_unit, i.get_status_display(),
              " ".join(i.flags), i.paid_at, i.payment_reference] for i in qs.iterator()))
    totals = qs.values("currency").annotate(total=Sum("amount_total")).order_by("currency")
    return render(request, "invoices/list.html", {"form": form, "page": paginate(request, qs), "totals": totals,
                                                  "sort": order, "today": timezone.localdate()})


@login_required
def invoice_create(request):
    require_any_role(request.user, [Role.CLERK, Role.UNIT_ADMIN])
    form = InvoiceForm(request.POST or None, request.FILES or None, user=request.user)
    prep = None
    if request.GET.get("preparation"):
        from operations.preparation import owned_preparation
        prep = owned_preparation(request.user, request.GET["preparation"])
        if prep.completed_batch_id:
            return redirect(prep.completed_batch)
        form.fields["service_account"].queryset = form.fields["service_account"].queryset.filter(
            org_unit__path__startswith=prep.org_unit.path, provider=prep.provider)
    if request.method == "POST" and form.is_valid():
        invoice = form.save(commit=False)
        if form.cleaned_data.get("upload"):
            _attach(invoice, form)
        try:
            save_invoice(invoice, request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"Invoice {invoice.reference} recorded.")
            if prep:
                return redirect(prep.get_absolute_url() + "?step=select")
            if invoice.flags:
                messages.warning(request, "Check the warnings on this invoice before submitting it.")
            return redirect(invoice)
    return render(request, "invoices/form.html", {"form": form, "title": "Record invoice"})


def _attach(invoice, form):
    f = form.cleaned_data["upload"]
    if invoice.document:
        invoice.document.delete(save=False)
    invoice.document = f
    invoice.document_name = f.name[:255]
    invoice.document_sha256 = form.upload_sha256


@login_required
def invoice_edit(request, pk):
    invoice = _get_invoice(request, pk)
    if not invoice.is_editable or not has_role(request.user, invoice.org_unit, [Role.CLERK, Role.UNIT_ADMIN]):
        raise PermissionDenied
    form = InvoiceForm(request.POST or None, request.FILES or None, instance=invoice, user=request.user)
    if request.method == "POST" and form.is_valid():
        invoice = form.save(commit=False)
        if form.cleaned_data.get("upload"):
            _attach(invoice, form)
        try:
            save_invoice(invoice, request.user, changed=form.changed_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Invoice updated.")
            return redirect(invoice)
    return render(request, "invoices/form.html", {"form": form, "title": f"Edit {invoice.reference}", "invoice": invoice})


@login_required
def invoice_detail(request, pk):
    from invoices.evidence import approval_evidence
    from signatures.models import Signature
    from signatures.services import signature_summary

    invoice = _get_invoice(request, pk)
    tasks = list(invoice.approval_tasks.select_related("decided_by").order_by("-round", "order"))
    signatures = {s.approval_task_id: s for s in Signature.objects.filter(approval_task__in=tasks)
                  .select_related("identity", "approval_task__invoice__provider",
                                  "approval_task__invoice__service_account", "approval_task__invoice__org_unit")}
    for t in tasks:
        t.sig = signature_summary(signatures[t.pk]) if t.pk in signatures else None
    rounds = {}
    for t in tasks:
        rounds.setdefault(t.round, []).append(t)
    roles = set(Role.values) if request.user.is_superuser else \
        {r for r in Role.values if has_role(request.user, invoice.org_unit, [r])}
    my_task = next((t for t in tasks if t.status == ApprovalTask.Status.PENDING), None)
    can_decide = False
    if my_task:
        try:
            engine.check_can_decide(my_task, request.user)
            can_decide = True
        except (PermissionDenied, ValidationError):
            pass
    history = AuditEvent.objects.filter(object_type="invoices.invoice", object_id=str(invoice.pk)).order_by("sequence")
    from analytics.queries import account_history

    batch_membership = invoice.batch_memberships.filter(active=True).select_related("batch").first()
    return render(request, "invoices/detail.html", {
        "invoice": invoice, "rounds": sorted(rounds.items(), reverse=True), "my_task": my_task if can_decide else None,
        "batch_membership": batch_membership,
        "pending_task": my_task, "can_capture": bool(roles & {Role.CLERK, Role.UNIT_ADMIN}),
        "flags": [(f, Flag.LABELS.get(f, f), f in Flag.SEVERE) for f in invoice.flags],
        "has_severe": bool(set(invoice.flags) & Flag.SEVERE),
        "comments": invoice.comments.select_related("author"), "comment_form": CommentForm(),
        "history": history, "projection": engine.sla_projection(invoice),
        "account_chart": account_history(invoice.service_account, 12),
        "settlement_item": invoice.settlement_items.select_related("line__instruction").order_by("-pk").first(),
        "approval_evidence": approval_evidence(invoice),
    })


@login_required
def invoice_document(request, pk):
    invoice = _get_invoice(request, pk)
    if not invoice.document:
        raise Http404
    AuditEvent.record("invoice.document_viewed", obj=invoice)
    ext = invoice.document.name.rsplit(".", 1)[-1].lower()
    kind = "jpg" if ext in ("jpg", "jpeg") else ext
    return file_response(invoice.document, invoice.document_name or f"{invoice.reference}.{ext}",
                         CONTENT_TYPES.get(kind, "application/octet-stream"), inline=kind in ("pdf", "png", "jpg"))


@login_required
def invoice_approval_record(request, pk):
    from invoices.evidence import approval_evidence

    invoice = _get_invoice(request, pk)
    evidence = approval_evidence(invoice)
    if not evidence:
        raise Http404("No current approval record is available.")
    response = render(request, "invoices/approval_record.html", {"invoice": invoice, "evidence": evidence})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def invoice_approved_copy(request, pk):
    from invoices.evidence import approval_evidence, render_approved_copy

    invoice = _get_invoice(request, pk)
    evidence = approval_evidence(invoice)
    if not evidence:
        raise Http404("Only approved invoices have an approval copy.")
    try:
        data = render_approved_copy(invoice, evidence)
    except (ValidationError, OSError) as exc:
        reason = " ".join(exc.messages) if isinstance(exc, ValidationError) else "The source document is unavailable."
        messages.error(request, reason)
        return redirect("invoices:approval_record", pk=pk)
    import hashlib

    AuditEvent.record("invoice.approved_copy_downloaded", obj=invoice, actor=request.user, data={
        "copy_sha256": hashlib.sha256(data).hexdigest(), "source_sha256": evidence["source_sha256"],
        "generated_at": evidence["generated_at"].isoformat(),
        "batch": evidence["batch_reference"], "bank_reference": evidence["bank_reference"],
    })
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{invoice.reference}-approved-copy.pdf"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def invoice_submit(request, pk):
    invoice = _get_invoice(request, pk)
    member = invoice.batch_memberships.filter(active=True).select_related("batch").first()
    if not has_role(request.user, invoice.org_unit, [Role.CLERK, Role.UNIT_ADMIN]):
        raise PermissionDenied
    if member:
        messages.info(request, "Submit and review invoices from the complete batch workspace.")
        return redirect(member.batch)
    messages.error(request, "Prepare this invoice in a batch before starting approval.")
    return redirect(invoice)


@login_required
@require_POST
def invoice_cancel(request, pk):
    invoice = _get_invoice(request, pk)
    form = ReasonForm(request.POST)
    if invoice.status not in (Invoice.Status.DRAFT, Invoice.Status.RETURNED, Invoice.Status.DISPUTED) or \
            not has_role(request.user, invoice.org_unit, [Role.CLERK, Role.UNIT_ADMIN]):
        raise PermissionDenied
    if not form.is_valid():
        messages.error(request, "Give a reason for cancelling.")
        return redirect(invoice)
    invoice.status = Invoice.Status.CANCELLED
    invoice.save(update_fields=["status", "updated_at"])
    AuditEvent.record("invoice.cancelled", obj=invoice, data={"reason": form.cleaned_data["reason"]})
    messages.success(request, "Invoice cancelled.")
    return redirect(invoice)


@login_required
@require_POST
def invoice_comment(request, pk):
    invoice = _get_invoice(request, pk)
    form = CommentForm(request.POST)
    if form.is_valid():
        InvoiceComment.objects.create(invoice=invoice, author=request.user, body=form.cleaned_data["body"])
        AuditEvent.record("invoice.commented", obj=invoice)
    return redirect(invoice.get_absolute_url() + "#comments")
