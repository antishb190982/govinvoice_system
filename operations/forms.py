from decimal import Decimal
from django import forms
from django.utils import timezone
from invoices.models import Invoice
from org.access import scope, units_for
from operations.models import DisputeCase
from operations.services import PROCESSORS
from providers.models import Provider


class BatchForm(forms.Form):
    name = forms.CharField(max_length=140)
    org_unit = forms.ModelChoiceField(queryset=None, label="Ministry / processing unit")
    provider = forms.ModelChoiceField(queryset=Provider.objects.all(), label="Single utility provider")
    currency = forms.RegexField(regex=r"^[A-Z]{3}$", max_length=3, initial="MUR")
    invoices = forms.ModelMultipleChoiceField(queryset=Invoice.objects.none(),
        widget=forms.CheckboxSelectMultiple, help_text="Select unbatched invoices from this provider and unit.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user, PROCESSORS)
        data = self.data if self.is_bound else self.initial
        unit, provider = str(data.get("org_unit", "")), str(data.get("provider", ""))
        if unit.isdigit() and provider.isdigit():
            chosen = self.fields["org_unit"].queryset.filter(pk=unit).first()
            if chosen:
                self.fields["invoices"].queryset = scope(Invoice.objects.filter(
                    org_unit__path__startswith=chosen.path, provider_id=provider,
                    currency=data.get("currency", "MUR")).exclude(
                    status__in=["paid", "closed_zero", "cancelled", "scheduled"]).exclude(
                    batch_memberships__active=True), user)


class CaseForm(forms.Form):
    is_surcharge = forms.BooleanField(required=False, label="This case concerns the tracked invoice surcharge",
        help_text="Uses the recorded surcharge as the item limit; it is already included in the billed total.")
    invoice = forms.ModelChoiceField(queryset=None)
    kind = forms.ChoiceField(choices=DisputeCase.Kind.choices)
    reason = forms.CharField(max_length=4000, widget=forms.Textarea)
    amount = forms.DecimalField(max_digits=16, decimal_places=3, min_value=0, initial=0,
        label="Requested waiver / refund or disputed amount")
    item_description = forms.CharField(max_length=250, required=False,
        label="Invoice item / charge (blank for whole invoice)")
    item_amount = forms.DecimalField(max_digits=16, decimal_places=3, min_value=0, required=False,
        label="Original amount of this item")
    evidence = forms.FileField(required=False, help_text="Supporting evidence, retained privately.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["invoice"].queryset = scope(Invoice.objects.select_related("provider"), user)


class RefundForm(forms.Form):
    amount = forms.DecimalField(max_digits=16, decimal_places=3, min_value=Decimal("0.001"), label="Amount actually received")
    reference = forms.CharField(max_length=120, label="Bank refund transaction reference")
    received_date = forms.DateField(initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    document = forms.FileField(label="Bank receipt / refund evidence")
