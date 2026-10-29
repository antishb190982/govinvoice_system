"""Surcharge classification is part of the billed gross, never a second charge."""
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from core.models import AuditEvent
from core.utils import paginate
from invoices.models import Invoice
from operations.models import DisputeCase, RefundReceipt
from org.access import has_role, scope
from org.models import Role

ZERO = Decimal("0")
RECORDERS = [Role.CLERK, Role.UNIT_ADMIN, Role.FINANCE]


def total(qs, field):
    return qs.aggregate(value=Sum(field))["value"] or ZERO


def summary(invoice):
    cases = invoice.dispute_cases.filter(is_surcharge=True)
    waived = total(cases.filter(applies_payable=True), "amount")
    refunded = total(RefundReceipt.objects.filter(case__in=cases), "amount")
    due = total(cases.filter(status__in=["refund_due", "refunded"]), "amount") - refunded
    return {"charged": invoice.surcharge_amount, "waived": waived, "received": refunded, "refund_due": due,
            "net_cost": invoice.surcharge_amount - waived - refunded,
            "pending_cases": cases.filter(status__in=["open", "authorisation", "refund_due"]).count()}


@transaction.atomic
def record(invoice, user, amount, description):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if not has_role(user, invoice.org_unit, RECORDERS):
        raise PermissionDenied
    if not amount.is_finite() or amount < ZERO or amount > invoice.amount_total:
        raise ValidationError("Surcharge must be non-negative and no greater than the original invoice total.")
    if invoice.dispute_cases.filter(is_surcharge=True).exists() and (
            amount != invoice.surcharge_amount or description != invoice.surcharge_description):
        raise ValidationError("A surcharge with dispute history cannot be rewritten; use the authorised case workflow.")
    before = {"amount": str(invoice.surcharge_amount), "description": invoice.surcharge_description}
    if amount != invoice.surcharge_amount or description != invoice.surcharge_description:
        invoice.surcharge_reviewed_by, invoice.surcharge_reviewed_at, invoice.surcharge_review_note = None, None, ""
    invoice.surcharge_amount, invoice.surcharge_description = amount, description[:240]
    invoice.save(update_fields=["surcharge_amount", "surcharge_description", "surcharge_reviewed_by",
                               "surcharge_reviewed_at", "surcharge_review_note", "updated_at"])
    AuditEvent.record("invoice.surcharge_recorded", obj=invoice, actor=user,
        data={"before": before, "amount": str(amount), "description": description[:240],
              "included_in_original_total": True, "original_payment_preserved": invoice.status == "paid"})
    return invoice


@transaction.atomic
def review(invoice, user, note):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if not has_role(user, invoice.org_unit, [Role.FINANCE]):
        raise PermissionDenied
    if invoice.surcharge_amount <= ZERO or not note.strip():
        raise ValidationError("A positive surcharge and a finance assessment are required.")
    invoice.surcharge_reviewed_by, invoice.surcharge_reviewed_at = user, timezone.now()
    invoice.surcharge_review_note = note.strip()[:4000]
    invoice.save(update_fields=["surcharge_reviewed_by", "surcharge_reviewed_at", "surcharge_review_note", "updated_at"])
    AuditEvent.record("invoice.surcharge_reviewed", obj=invoice, actor=user,
        data={"amount": str(invoice.surcharge_amount), "assessment": invoice.surcharge_review_note})
    return invoice


class ChargeForm(forms.Form):
    surcharge_amount = forms.DecimalField(max_digits=16, decimal_places=3, min_value=ZERO,
        label="Surcharge already included in billed total")
    surcharge_description = forms.CharField(max_length=240, required=False,
        label="Charge description / reason printed by provider")


@login_required
def manage(request, pk):
    invoice = get_object_or_404(scope(Invoice.objects.select_related("org_unit", "provider", "service_account",
        "surcharge_reviewed_by"), request.user), pk=pk)
    can_record = has_role(request.user, invoice.org_unit, RECORDERS)
    can_review = has_role(request.user, invoice.org_unit, [Role.FINANCE])
    form = ChargeForm(request.POST if request.method == "POST" and request.POST.get("action") == "record" else None,
        initial={"surcharge_amount": invoice.surcharge_amount, "surcharge_description": invoice.surcharge_description})
    error = ""
    if request.method == "POST":
        try:
            if request.POST.get("action") == "record":
                if not can_record:
                    raise PermissionDenied
                if form.is_valid():
                    record(invoice, request.user, form.cleaned_data["surcharge_amount"], form.cleaned_data["surcharge_description"])
                    messages.success(request, "Surcharge recorded without changing the invoice total.")
                    return redirect("invoices:surcharge", pk=pk)
            elif request.POST.get("action") == "review":
                review(invoice, request.user, request.POST.get("note", ""))
                messages.success(request, "Finance surcharge assessment recorded.")
                return redirect("invoices:surcharge", pk=pk)
            else:
                raise ValidationError("Unknown surcharge action.")
        except ValidationError as exc:
            error = " ".join(exc.messages)
    history = AuditEvent.objects.filter(object_type="invoices.invoice", object_id=str(invoice.pk),
                                       action__startswith="invoice.surcharge").order_by("-sequence")
    return render(request, "invoices/surcharge.html", {"invoice": invoice, "form": form, "error": error,
        "fee": summary(invoice), "can_record": can_record, "can_review": can_review,
        "cases": invoice.dispute_cases.filter(is_surcharge=True), "history": paginate(request, history, 30)})


@login_required
def monitor(request):
    qs = scope(Invoice.objects.filter(surcharge_amount__gt=ZERO).select_related(
        "provider", "service_account", "org_unit", "surcharge_reviewed_by"), request.user)
    q = request.GET.get("q", "").strip()[:120]
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(provider__name__icontains=q) |
            Q(service_account__account_number__icontains=q) | Q(surcharge_description__icontains=q))
    status = request.GET.get("status", "")
    if status == "unreviewed":
        qs = qs.filter(surcharge_reviewed_at__isnull=True)
    elif status == "reviewed":
        qs = qs.filter(surcharge_reviewed_at__isnull=False)
    elif status == "disputed":
        qs = qs.filter(pk__in=DisputeCase.objects.filter(is_surcharge=True,
            status__in=["open", "authorisation", "refund_due"]).values("invoice_id"))
    currency_options = list(qs.order_by().values_list("currency", flat=True).distinct())
    selected_currency = request.GET.get("currency", "").upper()
    if selected_currency in currency_options:
        qs = qs.filter(currency=selected_currency)
    cases = DisputeCase.objects.filter(invoice__in=qs, is_surcharge=True)
    charged = total(qs, "surcharge_amount")
    waived = total(cases.filter(applies_payable=True), "amount")
    received = total(RefundReceipt.objects.filter(case__in=cases), "amount")
    due = total(cases.filter(status__in=["refund_due", "refunded"]), "amount") - received
    providers = qs.values("provider__name", "provider__short_code", "currency").annotate(
        charged=Sum("surcharge_amount"), count=Count("pk")).order_by("-charged")[:30]
    accounts = qs.values("service_account__account_number", "service_account_id", "provider__short_code",
        "service_account__owner", "currency").annotate(charged=Sum("surcharge_amount"), count=Count("pk")).order_by("-charged")[:30]
    months = qs.annotate(month=TruncMonth("issue_date")).values("month", "currency").annotate(
        charged=Sum("surcharge_amount"), count=Count("pk")).order_by("-month")[:24]
    # Do not combine currencies into an apparently meaningful monetary total.
    currencies = list(qs.order_by().values_list("currency", flat=True).distinct())
    return render(request, "invoices/surcharges.html", {"page": paginate(request, qs.order_by("-issue_date"), 30),
        "q": q, "status": status, "providers": providers, "accounts": accounts, "months": months,
        "charged": charged if len(currencies) <= 1 else None, "waived": waived if len(currencies) <= 1 else None,
        "received": received if len(currencies) <= 1 else None, "refund_due": due if len(currencies) <= 1 else None,
        "net_cost": charged - waived - received if len(currencies) <= 1 else None,
        "currency": currencies[0] if len(currencies) == 1 else "", "mixed_currencies": len(currencies) > 1,
        "currency_options": currency_options, "selected_currency": selected_currency})
