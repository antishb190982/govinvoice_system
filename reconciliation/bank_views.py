import csv
import io

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, F, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.utils import paginate, require_any_role
from org.access import has_role, scope, units_for, user_has_any_role
from reconciliation import bank
from reconciliation.models import BankRow, BankStatement
from settlements.models import SettlementInstruction


class BankUploadForm(forms.Form):
    org_unit = forms.ModelChoiceField(queryset=None, label="Finance unit")
    account_label = forms.CharField(max_length=100, label="Bank account alias",
        help_text="Use one stable, unique alias per bank account across uploads; not an account number.")
    currency = forms.RegexField(regex=r"^[A-Z]{3}$", max_length=3, initial="MUR")
    statement_date = forms.DateField(initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    document = forms.FileField(label="Bank debit CSV", help_text="Up to 10 MB / 10,000 transactions. Use the template below.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user, bank.MANAGERS)


def rows_for(user):
    return scope(BankRow.objects.select_related("statement__org_unit", "statement__uploaded_by",
                 "instruction", "checked_by", "reviewed_by"), user, bank.READERS, prefix="statement__org_unit__")


@login_required
def index(request):
    require_any_role(request.user, bank.READERS)
    rows = rows_for(request.user)
    counts = dict(rows.order_by().values("status").annotate(n=Count("pk", distinct=True)).values_list("status", "n"))
    q = request.GET.get("q", "").strip()[:120]
    status = request.GET.get("status", "exception")
    if q:
        rows = rows.filter(Q(transaction_id__icontains=q) | Q(settlement_reference__icontains=q) |
                           Q(bank_reference__icontains=q) | Q(statement__account_label__icontains=q))
    if status in BankRow.Status.values:
        rows = rows.filter(status=status)
    executed = scope(SettlementInstruction.objects.filter(status="executed"), request.user, bank.READERS)
    if q:
        executed = executed.filter(reference__icontains=q)
    pending = executed.order_by().annotate(
        line_count=Count("lines", distinct=True), matched_count=Count(
            "lines", filter=Q(lines__bank_allocation__row__status="matched"), distinct=True)
    ).filter(line_count__gt=F("matched_count"))
    recent = list(executed.order_by("-executed_at", "-pk")[:20])
    for instruction in recent:
        instruction.bank_check = bank.bank_status(instruction)
    return render(request, "reconciliation/bank_index.html", {
        "page": paginate(request, rows.order_by("-statement_id", "line_number"), 30),
        "statements": scope(BankStatement.objects.select_related("org_unit", "uploaded_by"), request.user,
                            bank.READERS)[:20], "counts": counts, "q": q, "status": status,
        "executed": recent, "pending_count": pending.count(),
        "can_manage": user_has_any_role(request.user, bank.MANAGERS),
    })


@login_required
def upload(request):
    require_any_role(request.user, bank.MANAGERS)
    form = BankUploadForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            statement = bank.upload(request.user, **form.cleaned_data)
            messages.success(request, "Bank debits checked against executed settlements. Exceptions require investigation; no payment status was changed.")
            return redirect(statement)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "reconciliation/bank_upload.html", {"form": form})


@login_required
def statement(request, pk):
    require_any_role(request.user, bank.READERS)
    item = get_object_or_404(scope(BankStatement.objects.select_related("org_unit", "uploaded_by"),
                                  request.user, bank.READERS), pk=pk)
    return render(request, "reconciliation/bank_statement.html", {
        "statement": item, "page": paginate(request, item.rows.select_related("instruction", "checked_by"), 40),
        "counts": dict(item.rows.order_by().values("status").annotate(n=Count("pk")).values_list("status", "n")),
    })


@login_required
def row(request, pk):
    require_any_role(request.user, bank.READERS)
    item = get_object_or_404(rows_for(request.user), pk=pk)
    from core.models import AuditEvent
    from reconciliation.cases import CaseForm, update_case
    can_assign = has_role(request.user, item.statement.org_unit, bank.MANAGERS)
    case_form = CaseForm(
        request.POST if request.method == "POST" and request.POST.get("action") == "assign_case" else None,
        row=item) if can_assign and item.status == BankRow.Status.EXCEPTION else None
    if request.method == "POST" and request.POST.get("action") == "assign_case":
        if not can_assign:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        if case_form is None:
            messages.error(request, "This transaction is no longer an unresolved exception.")
            return redirect(item)
        if case_form.is_valid():
            try:
                update_case(item, request.user, case_form.cleaned_data["assigned_to"],
                    case_form.cleaned_data["resolution_due_date"], case_form.cleaned_data["note"])
                messages.success(request, "Exception ownership and deadline saved. No payment or matching status was changed.")
                return redirect(item)
            except ValidationError as exc:
                case_form.add_error(None, exc)
    error = ""
    if request.method == "POST" and request.POST.get("action") != "assign_case":
        reason = request.POST.get("reason", "").strip()
        if len(reason) > 2000:
            error = "Review reason must not exceed 2,000 characters."
        else:
            try:
                checked = bank.recheck(item, request.user, accept_date=request.POST.get("accept_date") == "on", reason=reason)
                if checked.status == BankRow.Status.MATCHED:
                    messages.success(request, "Bank line reconciled. Payment and invoice records were not changed.")
                elif checked.status == BankRow.Status.DUPLICATE:
                    messages.info(request, "Previously reconciled transaction. No duplicate allocation was made.")
                else:
                    messages.warning(request, f"Still needs investigation: {checked.reason}")
                return redirect(item)
            except ValidationError as exc:
                error = " ".join(exc.messages)
    return render(request, "reconciliation/bank_row.html", {
        "case_form": case_form,
        "case_events": AuditEvent.objects.filter(object_type=item._meta.label_lower,
            object_id=str(item.pk), action="bank.case_updated").order_by("-sequence")[:30],
        "row": item, "error": error, "can_manage": has_role(request.user, item.statement.org_unit, bank.MANAGERS),
        "can_review_date": request.user.pk != item.statement.uploaded_by_id and
                           has_role(request.user, item.statement.org_unit, bank.MANAGERS),
        "allocations": item.allocations.select_related("line__provider"),
        "bank_state": bank.bank_status(item.instruction) if item.instruction else None,
    })


@login_required
def download(request, pk):
    require_any_role(request.user, bank.READERS)
    item = get_object_or_404(scope(BankStatement.objects.all(), request.user, bank.READERS), pk=pk)
    return FileResponse(item.document.open("rb"), as_attachment=True, filename=item.filename)


@login_required
def template(request):
    require_any_role(request.user, bank.READERS)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(bank.HEADERS)
    writer.writerow(["BANK-TXN-001", "SI-2026-000001", "", "1234.50", "MUR",
                     timezone.localdate().isoformat(), "BANK-EXEC-REF"])
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="bank-debits-template.csv"'
    return response
