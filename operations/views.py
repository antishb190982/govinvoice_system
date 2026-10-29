from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from core.models import AuditEvent
from core.utils import paginate, require_any_role
from invoices.models import Invoice
from operations import services
from operations.forms import BatchForm, CaseForm, RefundForm
from operations.models import DisputeCase, InvoiceBatch
from org.access import has_role, scope
from org.models import Role


def events(batch=None, case=None):
    if case:
        return AuditEvent.objects.filter(object_type="operations.disputecase", object_id=str(case.pk)).order_by("-sequence")
    ids = list(batch.members.values_list("invoice_id", flat=True))
    cases = DisputeCase.objects.filter(invoice_id__in=ids).values_list("pk", flat=True)
    settlements = batch.settlements.values_list("pk", flat=True)
    return AuditEvent.objects.filter(
        Q(object_type="operations.invoicebatch", object_id=str(batch.pk)) |
        Q(object_type="invoices.invoice", object_id__in=[str(i) for i in ids]) |
        Q(object_type="operations.disputecase", object_id__in=[str(i) for i in cases]) |
        Q(object_type="settlements.settlementinstruction", object_id__in=[str(i) for i in settlements])
    ).order_by("-sequence")


@login_required
def batches(request):
    qs = scope(InvoiceBatch.objects.select_related("provider", "ministry", "org_unit", "created_by"), request.user)
    q = request.GET.get("q", "").strip()[:120]
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(name__icontains=q) | Q(provider__name__icontains=q))
    return render(request, "operations/batches.html", {"page": paginate(request, qs), "q": q})


@login_required
def batch_new(request):
    require_any_role(request.user, services.PROCESSORS)
    form = BatchForm(request.POST or None, user=request.user, initial=request.GET)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            return redirect(services.create_batch(request.user, d["org_unit"], d["name"], list(d["invoices"].values_list("pk", flat=True))))
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "operations/batch_new.html", {"form": form})


@login_required
def batch(request, pk):
    item = get_object_or_404(scope(InvoiceBatch.objects.select_related("ministry", "provider", "org_unit"),
                                   request.user), pk=pk)
    members = list(item.members.select_related("invoice__org_unit", "invoice__provider").order_by("invoice__due_date"))
    for member in members:
        member.payable = services.payable(member.invoice)
        member.on_hold = member.invoice.status != "paid" and (
            member.invoice.dispute_cases.filter(status__in=services.LIVE_CASES).exists() or
            member.invoice.service_account.status != "active")
    error = ""
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if action == "submit":
                services.start_batch(item, request.user)
                messages.success(request, "Batch submitted atomically to the existing invoice approval workflows.")
            elif action == "cancel":
                services.cancel_batch(item, request.user, request.POST.get("reason", ""))
                messages.success(request, "Unstarted batch cancelled; membership history retained.")
            elif action == "release":
                from operations.regroup import release_member
                invoice_id = request.POST.get("invoice_id", "")
                if not invoice_id.isdigit():
                    raise ValidationError("Choose a batch invoice.")
                release_member(item, request.user, int(invoice_id), request.POST.get("reason", ""))
                messages.success(request, "Invoice released for correction or reassignment; batch history retained.")
            elif action == "settle":
                from settlements.services import create_instruction
                active = [member.invoice for member in members if member.active and member.payable > 0]
                if not active:
                    raise ValidationError("This batch has no active invoices.")
                instruction = create_instruction(request.user, item.org_unit, item.currency,
                    max(i.due_date for i in active), [i.pk for i in active], f"Batch {item.reference}", provider=item.provider)
                return redirect(instruction)
            else:
                raise ValidationError("Unknown batch action.")
            return redirect(item)
        except ValidationError as exc:
            error = " ".join(exc.messages)
    return render(request, "operations/batch.html", {
        "batch": item, "members": members, "error": error,
        "gross_total": sum((m.invoice.amount_total for m in members if m.active), Decimal("0")),
        "payable_total": sum((m.payable for m in members if m.active), Decimal("0")),
        "held_count": sum(m.on_hold for m in members if m.active), "settlements": item.settlements.all(),
        "cases": DisputeCase.objects.filter(invoice__batch_memberships__batch=item).distinct(),
        "history": paginate(request, events(batch=item), 30),
        "can_process": has_role(request.user, item.org_unit, [Role.CLERK, Role.UNIT_ADMIN]) and not item.cancelled_at and
                       any(m.active and m.invoice.status in ["draft", "returned", "disputed"] for m in members),
        "can_regroup": has_role(request.user, item.org_unit, services.PROCESSORS) and not item.cancelled_at and
                       not item.settlements.exclude(status="cancelled").exists() and
                       any(m.active and m.invoice.status not in ["paid", "scheduled"] for m in members),
        "can_settle": has_role(request.user, item.org_unit, [Role.FINANCE]) and not item.cancelled_at and
                      any(m.active and m.payable > 0 for m in members) and
                      all((m.invoice.status == "paid" and m.payable == 0) or
                          (m.invoice.status == "approved" and not m.on_hold and m.payable > 0) for m in members if m.active) and
                      not item.settlements.exclude(status="cancelled").exists(),
        "can_cancel": has_role(request.user, item.org_unit, services.PROCESSORS) and not item.started_at and not item.cancelled_at,
    })


@login_required
def cases(request):
    qs = scope(DisputeCase.objects.select_related("invoice__provider", "org_unit", "opened_by"), request.user)
    status = request.GET.get("status", "active")
    if status == "active":
        qs = qs.filter(status__in=services.LIVE_CASES)
    elif status in DisputeCase.Status.values:
        qs = qs.filter(status=status)
    q = request.GET.get("q", "").strip()[:120]
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(invoice__reference__icontains=q) |
                       Q(invoice__invoice_number__icontains=q) | Q(item_description__icontains=q))
    return render(request, "operations/cases.html", {"page": paginate(request, qs), "q": q,
        "status": status, "statuses": DisputeCase.Status.choices,
        "legacy_disputes": scope(Invoice.objects.filter(status="disputed", dispute_cases__isnull=True), request.user)})


@login_required
def case_new(request):
    require_any_role(request.user, services.OPENERS)
    form = CaseForm(request.POST or None, request.FILES or None, user=request.user,
                    initial={"invoice": request.GET.get("invoice"), "is_surcharge": request.GET.get("surcharge") == "1"})
    if request.method == "POST" and form.is_valid():
        try:
            return redirect(services.open_case(request.user, **form.cleaned_data))
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "operations/case_new.html", {"form": form})


@login_required
def case(request, pk):
    item = get_object_or_404(scope(DisputeCase.objects.select_related("invoice", "org_unit", "opened_by",
        "finance_by", "approved_by", "refund_by"), request.user), pk=pk)
    refund_form = RefundForm(request.POST if request.POST.get("action") == "refund" else None,
                             request.FILES or None)
    error = ""
    if request.method == "POST":
        action, note = request.POST.get("action"), request.POST.get("note", "").strip()
        try:
            if action == "endorse":
                services.endorse(item, request.user, note)
            elif action in ["approve", "reject"]:
                services.decide_case(item, request.user, action == "approve", note, request.POST.get("pin", ""))
            elif action == "withdraw":
                services.withdraw(item, request.user, note)
            elif action == "refund" and refund_form.is_valid():
                services.record_refund(item, request.user, **refund_form.cleaned_data)
            else:
                raise ValidationError("Complete the required action fields.")
            messages.success(request, "Case updated with an audit record. Original payment history was preserved.")
            return redirect(item)
        except ValidationError as exc:
            error = " ".join(exc.messages)
    return render(request, "operations/case.html", {"case": item, "error": error, "refund_form": refund_form,
        "payable": services.payable(item.invoice), "history": paginate(request, events(case=item), 30),
        "receipts": item.refund_receipts.all(),
        "received_total": sum((r.amount for r in item.refund_receipts.all()), Decimal("0")),
        "can_finance": has_role(request.user, item.org_unit, [Role.FINANCE]),
        "can_authorise": has_role(request.user, item.org_unit, [Role.AUTHORISER]) and
                         request.user.pk not in [item.opened_by_id, item.finance_by_id],
        "can_withdraw": item.status in ["open", "authorisation"] and
                        (request.user.pk == item.opened_by_id or has_role(request.user, item.org_unit, [Role.FINANCE, Role.UNIT_ADMIN])),
    })


@login_required
def document(request, pk, kind):
    item = get_object_or_404(scope(DisputeCase.objects.all(), request.user), pk=pk)
    field = item.evidence if kind == "evidence" else item.refund_document if kind == "refund" else None
    if kind.startswith("receipt-") and kind[8:].isdigit():
        receipt = item.refund_receipts.filter(pk=kind[8:]).first()
        field = receipt.document if receipt else None
    if not field:
        from django.http import Http404
        raise Http404
    return FileResponse(field.open("rb"), as_attachment=True, filename=field.name.rsplit("/", 1)[-1])
