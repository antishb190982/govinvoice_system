from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.utils import paginate, require_any_role
from invoices.models import Invoice
from org.access import has_role, scope, user_has_any_role
from org.models import Role
from reconciliation import services
from reconciliation.forms import AssignmentForm, UploadForm
from reconciliation.models import Statement, StatementRow


@login_required
def index(request):
    statements = scope(Statement.objects.select_related("provider", "org_unit", "uploaded_by"), request.user)
    rows = scope(StatementRow.objects.filter(statement__is_active=True).select_related(
        "statement__provider", "assigned_unit", "assigned_to", "invoice"), request.user, prefix="assigned_unit__")
    counts = dict(rows.order_by().values("status").annotate(n=Count("pk", distinct=True)).values_list("status", "n"))
    q = request.GET.get("q", "").strip()[:100]
    if q:
        rows = rows.filter(Q(invoice_number__icontains=q) | Q(account_number__icontains=q) |
                           Q(invoice__reference__icontains=q) | Q(statement__provider__name__icontains=q))
    state = request.GET.get("state", "unsorted")
    if state in StatementRow.Status.values:
        rows = rows.filter(status=state)
    checked = None
    if q:
        invoice = scope(Invoice.objects.select_related("provider", "service_account", "org_unit"),
                        request.user).filter(Q(reference__iexact=q) | Q(invoice_number__iexact=q)).first()
        if invoice:
            checked = {"invoice": invoice, **services.report(invoice)}
    return render(request, "reconciliation/index.html", {
        "statements": statements.annotate(row_count=Count("rows")).order_by("-statement_date", "-pk")[:30],
        "page": paginate(request, rows.order_by("-statement_id", "line_number"), 30),
        "counts": counts, "q": q, "state": state, "checked": checked,
        "can_upload": user_has_any_role(request.user, services.MANAGERS),
    })


@login_required
def upload(request):
    require_any_role(request.user, services.MANAGERS)
    form = UploadForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            statement = services.upload_statement(request.user, **form.cleaned_data)
            messages.success(request, "Statement reconciled. Mismatches remain in the Unsorted basket; no invoice was approved or paid.")
            return redirect(statement)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "reconciliation/upload.html", {"form": form})


@login_required
def statement_detail(request, pk):
    statement = get_object_or_404(scope(Statement.objects.select_related("provider", "org_unit", "uploaded_by"),
                                       request.user), pk=pk)
    counts = dict(statement.rows.order_by().values("status").annotate(n=Count("pk")).values_list("status", "n"))
    rows = statement.rows.select_related("assigned_unit", "assigned_to", "invoice").order_by("line_number")
    return render(request, "reconciliation/statement.html", {
        "statement": statement, "counts": counts, "page": paginate(request, rows, 40),
        "can_manage": has_role(request.user, statement.org_unit, services.MANAGERS),
    })


@login_required
def row_detail(request, pk):
    row = get_object_or_404(scope(StatementRow.objects.select_related(
        "statement__provider", "statement__org_unit", "assigned_unit", "assigned_to", "invoice__org_unit"),
        request.user, prefix="assigned_unit__"), pk=pk)
    form = AssignmentForm(request.POST if request.POST.get("action") == "assign" else None,
                          user=request.user, row=row, chosen_unit=request.GET.get("org_unit"))
    error = ""
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if action == "recheck":
                services.recheck(row, request.user)
                messages.success(request, "Rechecked against the recorded invoice. See the result below.")
                return redirect(row)
            if action == "assign" and form.is_valid():
                data = form.cleaned_data
                services.assign(row, request.user, data["org_unit"], data["assigned_to"], data["reason"])
                messages.success(request, "Responsibility updated and statement line rechecked.")
                return redirect(row)
            if action == "return":
                services.return_for_correction(row, request.user, request.POST.get("reason", ""))
                messages.success(request, "Returned to invoice capture for correction and a new approval round. Prior decisions remain in history.")
                return redirect(row)
        except ValidationError as exc:
            error = " ".join(exc.messages)
    return render(request, "reconciliation/row.html", {
        "row": row, "form": form, "error": error,
        "differences": {
            "account": bool(row.invoice and services.norm(row.invoice.service_account.account_number) != services.norm(row.account_number)),
            "invoice": bool(row.invoice and services.norm(row.invoice.invoice_number) != services.norm(row.invoice_number)),
            "amount": bool(row.invoice and row.invoice.amount_total != row.amount),
        },
        "can_manage": has_role(request.user, row.assigned_unit, services.MANAGERS),
        "can_recheck": has_role(request.user, row.assigned_unit, services.MANAGERS + [Role.CLERK]),
        "can_return": bool(row.invoice and row.status == "unsorted" and row.invoice.status in ["approved", "in_approval", "disputed"]
                           and has_role(request.user, row.invoice.org_unit, services.MANAGERS)),
    })


@login_required
def download(request, pk):
    statement = get_object_or_404(scope(Statement.objects.all(), request.user), pk=pk)
    if not statement.document:
        from django.http import Http404
        raise Http404
    return FileResponse(statement.document.open("rb"), as_attachment=True, filename=statement.filename)


@login_required
def template(request):
    response = HttpResponse("account_number,invoice_number,amount,currency\r\nACC-001,INV-001,1234.50,MUR\r\n",
                            content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="provider-pending-invoices-template.csv"'
    return response
