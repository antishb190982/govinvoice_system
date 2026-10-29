from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import AuditEvent
from core.utils import csv_response, file_response, paginate, require_any_role
from org.access import can_view, has_role, scope, units_for
from org.models import Role
from settlements import services
from settlements.models import SettlementInstruction
from providers.models import Provider
from signatures.services import active_identity


class NewInstructionForm(forms.Form):
    provider = forms.ModelChoiceField(queryset=Provider.objects.all(), label="Single utility provider")
    org_unit = forms.ModelChoiceField(queryset=None, label="Paying unit (ministry / authority)")
    currency = forms.CharField(max_length=3, initial=settings.DEFAULT_CURRENCY)
    cutoff_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                                  label="Include invoices due on or before")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user, [Role.FINANCE])
        self.fields["org_unit"].label_from_instance = lambda u: u.indented_name
        if "cutoff_date" in self.fields:
            self.fields["cutoff_date"].initial = timezone.localdate() + timedelta(days=14)


class PinForm(forms.Form):
    pin = forms.CharField(label="Signing PIN", widget=forms.PasswordInput(attrs={"autocomplete": "off"}))


class SettlementGroupForm(NewInstructionForm):
    cutoff_date = None


class ExecutedForm(forms.Form):
    bank_reference = forms.CharField(max_length=80)
    value_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), initial=timezone.localdate)


def _errors(exc):
    return " ".join(getattr(exc, "messages", [str(exc)]))


def _get(request, pk):
    si = get_object_or_404(SettlementInstruction.objects.select_related("org_unit", "prepared_by", "authorised_by"), pk=pk)
    if not can_view(request.user, si.org_unit):
        raise Http404
    return si


@login_required
def instruction_list(request):
    qs = scope(SettlementInstruction.objects.select_related("org_unit", "prepared_by"), request.user)
    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)
    if request.GET.get("groups") == "1":
        qs = qs.filter(group_reference__isnull=False)
    return render(request, "settlements/list.html", {"page": paginate(request, qs), "status": status,
        "groups_only": request.GET.get("groups") == "1", "statuses": SettlementInstruction.Status.choices})


@login_required
def group_new(request):
    from settlements.grouping import create_group, ready_batches

    require_any_role(request.user, [Role.FINANCE])
    form = SettlementGroupForm(request.GET or None, user=request.user)
    rows = None
    selected = request.POST.getlist("batch")
    if form.is_valid():
        d = form.cleaned_data
        currency = d["currency"].upper()
        if request.method == "POST":
            try:
                if not selected or any(not value.isdigit() for value in selected):
                    raise ValidationError("Select at least one approved batch.")
                si = create_group(request.user, d["org_unit"], d["provider"], currency,
                                  selected, request.POST.get("notes", "")[:2000])
                messages.success(request, f"{si.group_reference} created. Review all member batches before signing {si.reference}.")
                return redirect(si)
            except (ValidationError, PermissionDenied) as exc:
                messages.error(request, _errors(exc))
        rows = ready_batches(request.user, d["org_unit"], d["provider"], currency)
    return render(request, "settlements/group_new.html", {
        "form": form, "rows": rows, "selected": selected,
        "total": sum(row["amount"] for row in rows) if rows else 0,
    })


@login_required
def instruction_new(request):
    require_any_role(request.user, [Role.FINANCE])
    form = NewInstructionForm(request.GET or None, user=request.user)
    lines = None
    if form.is_valid():
        d = form.cleaned_data
        if request.method == "POST":
            ids = [int(i) for i in request.POST.getlist("invoice") if i.isdigit()]
            try:
                si = services.create_instruction(request.user, d["org_unit"], d["currency"].upper(), d["cutoff_date"],
                                                 ids, request.POST.get("notes", "")[:2000], provider=d["provider"])
                messages.success(request, f"{si.reference} created. Review it, then sign and submit for authorisation.")
                return redirect(si)
            except (ValidationError, PermissionDenied) as exc:
                messages.error(request, _errors(exc))
        lines = services.preview(d["org_unit"], d["currency"].upper(), d["cutoff_date"], provider=d["provider"])
    return render(request, "settlements/new.html", {"form": form, "lines": lines,
                                                    "grand_total": sum(li["amount"] for li in lines) if lines else 0})


@login_required
def instruction_detail(request, pk):
    si = _get(request, pk)
    user = request.user
    lines = si.lines.select_related("provider").prefetch_related("items__invoice__org_unit")
    return render(request, "settlements/detail.html", {
        "si": si, "lines": lines, "signings": si.signings.select_related("user", "identity"),
        "pin_form": PinForm(), "executed_form": ExecutedForm(),
        "can_prepare": has_role(user, si.org_unit, [Role.FINANCE]),
        "can_authorise": has_role(user, si.org_unit, [Role.AUTHORISER]) and user.pk != si.prepared_by_id,
        "identity": active_identity(user),
        "history": AuditEvent.objects.filter(object_type="settlements.settlementinstruction",
                                             object_id=str(si.pk)).order_by("sequence"),
        "batch_links": si.batch_links.select_related("batch__provider", "batch__ministry"),
    })


@login_required
@require_POST
def instruction_action(request, pk, action):
    si = _get(request, pk)
    user = request.user
    try:
        if action == "submit":
            form = PinForm(request.POST)
            if form.is_valid():
                services.submit_for_authorisation(si, user, form.cleaned_data["pin"])
                messages.success(request, "Signed and sent for authorisation.")
        elif action == "authorise":
            form = PinForm(request.POST)
            if form.is_valid():
                services.authorise(si, user, form.cleaned_data["pin"])
                messages.success(request, "Instruction authorised and counter-signed.")
        elif action == "reject":
            services.reject(si, user, request.POST.get("comment", ""))
            messages.success(request, "Returned to the preparer.")
        elif action == "cancel":
            services.cancel(si, user, request.POST.get("comment", "")[:500] or "Cancelled")
            messages.success(request, "Instruction cancelled; its invoices are available again.")
        elif action == "sent":
            services.mark_sent(si, user)
            messages.success(request, "Marked as sent to the bank / treasury.")
        elif action == "executed":
            form = ExecutedForm(request.POST)
            if form.is_valid():
                services.mark_executed(si, user, form.cleaned_data["bank_reference"], form.cleaned_data["value_date"])
                messages.success(request, "Payment recorded; invoices marked as paid.")
            else:
                messages.error(request, "Enter the bank reference and value date.")
        else:
            raise Http404
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, _errors(exc))
    return redirect(si)


@login_required
def instruction_pdf(request, pk):
    si = _get(request, pk)
    if not si.pdf:
        raise Http404
    AuditEvent.record("settlement.pdf_downloaded", obj=si)
    return file_response(si.pdf, f"{si.reference}.pdf", "application/pdf", inline=False)


@login_required
def instruction_export(request, pk, fmt):
    si = _get(request, pk)
    if si.status not in (SettlementInstruction.Status.AUTHORISED, SettlementInstruction.Status.SENT,
                         SettlementInstruction.Status.EXECUTED):
        raise PermissionDenied("Only authorised instructions can be exported.")
    if not has_role(request.user, si.org_unit, [Role.FINANCE, Role.AUTHORISER]):
        raise PermissionDenied
    AuditEvent.record("settlement.exported", obj=si, data={"format": fmt})
    if fmt == "xml":
        response = HttpResponse(services.pain001(si), content_type="application/xml")
        response["Content-Disposition"] = f'attachment; filename="{si.reference}-pain001.xml"'
        return response
    return csv_response(f"{si.reference}.csv",
                        ["End-to-end ID", "Beneficiary", "IBAN", "BIC", "Currency", "Amount", "Invoices", "Pay by"],
                        ([li.end_to_end_id, li.beneficiary_name, li.beneficiary_iban, li.beneficiary_bic, si.currency,
                          li.amount, li.invoice_count, li.earliest_due] for li in si.lines.all()))
