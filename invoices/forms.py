from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.files import validate_upload
from invoices.models import Invoice
from org.access import units_for
from org.models import Role
from providers.models import ServiceAccount, UtilityType

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class InvoiceForm(forms.ModelForm):
    upload = forms.FileField(required=False, label="Provider invoice (PDF or image)",
                             help_text="Required before submitting for approval.")

    class Meta:
        model = Invoice
        fields = ["service_account", "invoice_number", "issue_date", "received_date", "due_date", "period_start", "period_end",
                  "currency", "amount_net", "amount_tax", "amount_total", "surcharge_amount", "surcharge_description", "arrears", "previous_reading", "current_reading",
                  "consumption", "notes"]
        widgets = {"issue_date": DATE, "received_date": DATE, "due_date": DATE, "period_start": DATE, "period_end": DATE,
                   "notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"amount_net": "Net amount", "amount_tax": "Tax / VAT", "amount_total": "Original billed total"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["surcharge_amount"].required = False
        self.fields["surcharge_amount"].help_text = "Classification only: already included in the original total. Do not add it twice."

        self.fields["received_date"].required = True
        if not self.instance.pk:
            self.fields["received_date"].initial = timezone.localdate
        units = units_for(user, [Role.CLERK, Role.UNIT_ADMIN])
        self.fields["service_account"].queryset = ServiceAccount.objects.filter(
            org_unit__in=units).select_related("provider", "org_unit")
        self.fields["service_account"].label_from_instance = \
            lambda a: f"{a.provider.short_code} · {a.account_number} · {a.premises or a.description} ({a.org_unit.code})"
        self.fields["currency"].required = False
        self.fields["currency"].help_text = "Defaults to the provider's currency."
        self.fields["amount_net"].required = False
        self.fields["amount_tax"].required = False

    def clean_surcharge_amount(self):
        from decimal import Decimal
        if self.is_bound and "surcharge_amount" not in self.data:
            return self.instance.surcharge_amount
        return self.cleaned_data.get("surcharge_amount") or Decimal("0")

    def clean_upload(self):
        f = self.cleaned_data.get("upload")
        if f:
            kind, digest = validate_upload(f, {"pdf", "png", "jpg"})
            self.upload_kind, self.upload_sha256 = kind, digest
        return f

    def clean(self):
        data = super().clean()
        for field in ("amount_net", "amount_tax"):
            if data.get(field) is None:
                data[field] = 0
        return data

    def _post_clean(self):
        super()._post_clean()
        # Model-level business rules (duplicates, dates) shown on the form.
        from invoices.services import validate_business_rules

        if self.instance.service_account_id and not self.errors:
            self.instance.provider = self.instance.service_account.provider
            try:
                validate_business_rules(self.instance)
            except ValidationError as exc:
                self._update_errors(exc)


class InvoiceFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(required=False, choices=[("", "Any status"), ("open", "All unpaid"),
        ("billed", "Billed (excluding draft / cancelled)")] + list(Invoice.Status.choices))
    provider = forms.ChoiceField(required=False, choices=[])
    currency = forms.RegexField(regex=r"^[A-Z]{3}$", max_length=3, required=False, label="Currency")
    utility = forms.ChoiceField(required=False, choices=[("", "Any utility")] + list(UtilityType.choices))
    org_unit = forms.ModelChoiceField(required=False, queryset=None, empty_label="All my units")
    due_from = forms.DateField(required=False, widget=DATE)
    due_to = forms.DateField(required=False, widget=DATE)
    flagged = forms.BooleanField(required=False, label="Flagged only")
    priority = forms.ChoiceField(required=False, choices=[
        ("", "Any priority"), ("urgent", "Overdue / urgent"), ("high", "High"), ("normal", "Normal")
    ])

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        from org.access import scope

        self.fields["provider"].choices = [("", "Any provider")] + list(
            scope(Invoice.objects.all(), user).order_by("provider__name").values_list(
                "provider_id", "provider__name").distinct())
        self.fields["org_unit"].label_from_instance = lambda u: u.indented_name


class CommentForm(forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Add a note for the file…"}),
                           max_length=2000, label="")


class ReasonForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), max_length=1000)
