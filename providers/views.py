import re

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import User
from analytics.queries import account_history
from core.models import AuditEvent
from core.notify import notify
from invoices.models import Invoice
from org.access import can_view, has_role, scope, units_for, user_has_any_role
from org.models import OrgUnit, Role
from providers.models import BankDetailsChange, Provider, ServiceAccount


def iban_is_valid(iban):
    iban = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    digits = "".join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    return int(digits) % 97 == 1


def is_master_data_admin(user):
    """Provider master data is shared government-wide: top-level unit admins only."""
    if user.is_superuser:
        return True
    return any(has_role(user, unit, [Role.UNIT_ADMIN]) for unit in OrgUnit.objects.filter(parent__isnull=True))


class ProviderForm(forms.ModelForm):
    class Meta:
        model = Provider
        fields = ["name", "short_code", "utility_type", "tax_number", "contact_email", "contact_phone",
                  "payment_terms_days", "currency", "is_active"]


class BankChangeForm(forms.ModelForm):
    class Meta:
        model = BankDetailsChange
        fields = ["bank_name", "bank_bic", "bank_iban", "bank_account_name", "evidence_note"]
        widgets = {"evidence_note": forms.Textarea(attrs={"rows": 3}), "bank_iban": forms.TextInput()}
        labels = {"bank_iban": "IBAN", "bank_bic": "BIC / SWIFT", "bank_account_name": "Beneficiary account name"}

    def clean_bank_iban(self):
        iban = self.cleaned_data["bank_iban"].replace(" ", "").upper()
        if not iban_is_valid(iban):
            raise ValidationError("This IBAN fails the checksum.")
        return iban

    def clean_bank_bic(self):
        bic = self.cleaned_data["bank_bic"].strip().upper()
        if bic and not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", bic):
            raise ValidationError("Not a valid BIC.")
        return bic


class AccountForm(forms.ModelForm):
    class Meta:
        model = ServiceAccount
        fields = ["provider", "org_unit", "account_number", "meter_number", "description", "premises", "tariff",
                  "budget_line", "is_active"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user, [Role.UNIT_ADMIN])
        self.fields["org_unit"].label_from_instance = lambda u: u.indented_name


@login_required
def provider_list(request):
    providers = Provider.objects.annotate(
        accounts_count=Count("accounts", distinct=True),
        unpaid=Sum("invoices__amount_total", filter=Q(invoices__status__in=Invoice.UNPAID)),
    )
    return render(request, "providers/list.html", {"providers": providers, "can_edit": is_master_data_admin(request.user),
                                                   "pending_changes": BankDetailsChange.objects.filter(
                                                       status=BankDetailsChange.Status.PENDING).count()})


@login_required
def provider_detail(request, pk):
    provider = get_object_or_404(Provider, pk=pk)
    accounts = scope(provider.accounts.select_related("org_unit"), request.user)
    return render(request, "providers/detail.html", {
        "provider": provider, "accounts": accounts, "can_edit": is_master_data_admin(request.user),
        "can_add_account": user_has_any_role(request.user, [Role.UNIT_ADMIN]),
        "changes": provider.bank_changes.select_related("requested_by", "decided_by")[:10],
    })


@login_required
def provider_form(request, pk=None):
    if not is_master_data_admin(request.user):
        raise PermissionDenied
    provider = get_object_or_404(Provider, pk=pk) if pk else None
    form = ProviderForm(request.POST or None, instance=provider)
    if request.method == "POST" and form.is_valid():
        changed = form.changed_data
        provider = form.save()
        AuditEvent.record("provider.saved", obj=provider, data={"fields": changed})
        messages.success(request, "Provider saved." + ("" if pk else " Now submit its bank details."))
        return redirect("providers:detail", pk=provider.pk)
    return render(request, "providers/form.html", {"form": form, "provider": provider})


@login_required
def bank_change_request(request, pk):
    if not is_master_data_admin(request.user):
        raise PermissionDenied
    provider = get_object_or_404(Provider, pk=pk)
    form = BankChangeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        change = form.save(commit=False)
        change.provider = provider
        change.requested_by = request.user
        change.save()
        AuditEvent.record("provider.bank_change_requested", obj=provider,
                          data={"change": change.pk, "iban_last4": change.bank_iban[-4:]})
        admins = User.objects.filter(
            Q(is_superuser=True) | Q(memberships__role=Role.UNIT_ADMIN, memberships__org_unit__parent__isnull=True),
            is_active=True).exclude(pk=request.user.pk).distinct()
        notify(admins, f"Approve bank details change: {provider.name}", "A second administrator must approve.",
               link=f"/providers/{provider.pk}/", dedupe_key=f"bankchange-{change.pk}")
        messages.success(request, "Change requested. A different administrator must approve it.")
        return redirect("providers:detail", pk=provider.pk)
    return render(request, "providers/bank_change.html", {"form": form, "provider": provider})


@login_required
@require_POST
def bank_change_decide(request, pk, change_pk):
    if not is_master_data_admin(request.user):
        raise PermissionDenied
    change = get_object_or_404(BankDetailsChange, pk=change_pk, provider_id=pk, status=BankDetailsChange.Status.PENDING)
    if change.requested_by_id == request.user.pk:
        messages.error(request, "Dual control: you cannot approve your own change request.")
        return redirect("providers:detail", pk=pk)
    approve = request.POST.get("decision") == "approve"
    with transaction.atomic():
        change.status = BankDetailsChange.Status.APPROVED if approve else BankDetailsChange.Status.REJECTED
        change.decided_by = request.user
        change.decided_at = timezone.now()
        change.save()
        if approve:
            p = change.provider
            p.bank_name, p.bank_bic, p.bank_iban, p.bank_account_name = (
                change.bank_name, change.bank_bic, change.bank_iban, change.bank_account_name)
            p.save()
        AuditEvent.record("provider.bank_change_" + ("approved" if approve else "rejected"), obj=change.provider,
                          data={"change": change.pk, "requested_by": change.requested_by_id})
    messages.success(request, "Bank details updated." if approve else "Change rejected.")
    return redirect("providers:detail", pk=pk)


@login_required
def account_detail(request, pk):
    account = get_object_or_404(ServiceAccount.objects.select_related("provider", "org_unit"), pk=pk)
    if not can_view(request.user, account.org_unit):
        raise Http404
    return render(request, "providers/account.html", {
        "account": account, "chart": account_history(account, 24),
        "invoices": account.invoices.order_by("-issue_date")[:24],
        "can_edit": has_role(request.user, account.org_unit, [Role.UNIT_ADMIN]),
    })


@login_required
def account_form(request, pk=None):
    account = get_object_or_404(ServiceAccount, pk=pk) if pk else None
    if account and not has_role(request.user, account.org_unit, [Role.UNIT_ADMIN]):
        raise PermissionDenied
    if not user_has_any_role(request.user, [Role.UNIT_ADMIN]):
        raise PermissionDenied
    form = AccountForm(request.POST or None, instance=account, user=request.user,
                       initial={"provider": request.GET.get("provider")})
    if request.method == "POST" and form.is_valid():
        changed = form.changed_data
        account = form.save()
        AuditEvent.record("service_account.saved", obj=account, data={"fields": changed})
        messages.success(request, "Service account saved.")
        return redirect("providers:account", pk=account.pk)
    return render(request, "providers/account_form.html", {"form": form, "account": account})
