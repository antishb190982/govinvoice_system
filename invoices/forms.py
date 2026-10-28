from django import forms
from django.core.exceptions import ValidationError

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
        fields = ["service_account", "invoice_number", "issue_date", "due_date", "period_start", "period_end",
                  "currency", "amount_net", "amount_tax", "amount_total", "arrears", "previous_reading", "current_reading",
                  "consumption", "notes"]
        widgets = {"issue_date": DATE, "due_date": DATE, "period_start": DATE, "period_end": DATE,
                   "notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"amount_net": "Net amount", "amount_tax": "Tax / VAT", "amount_total": "Total payable"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        units = units_for(user, [Role.CLERK, Role.UNIT_ADMIN])
        self.fields["service_account"].queryset = ServiceAccount.objects.filter(
            is_active=True, org_unit__in=units).select_related("provider", "org_unit")
        self.fields["service_account"].label_from_instance = \
            lambda a: f"{a.provider.short_code} · {a.account_number} · {a.premises or a.description} ({a.org_unit.code})"
        self.fields["currency"].required = False
        self.fields["currency"].help_text = "Defaults to the provider's currency."
        self.fields["amount_net"].required = False
        self.fields["amount_tax"].required = False

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
    status = forms.ChoiceField(required=False, choices=[("", "Any status"), ("open", "All unpaid")] + list(Invoice.Status.choices))
    utility = forms.ChoiceField(required=False, choices=[("", "Any utility")] + list(UtilityType.choices))
    org_unit = forms.ModelChoiceField(required=False, queryset=None, empty_label="All my units")
    due_from = forms.DateField(required=False, widget=DATE)
    due_to = forms.DateField(required=False, widget=DATE)
    flagged = forms.BooleanField(required=False, label="Flagged only")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        self.fields["org_unit"].label_from_instance = lambda u: u.indented_name


class CommentForm(forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Add a note for the file…"}),
                           max_length=2000, label="")


class ReasonForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), max_length=1000)
