from django import forms
from django.utils import timezone

from accounts.models import User
from org.access import units_for, users_with_role
from providers.models import Provider
from reconciliation.services import MANAGERS
from org.models import Role


class UploadForm(forms.Form):
    provider = forms.ModelChoiceField(queryset=Provider.objects.all())
    org_unit = forms.ModelChoiceField(queryset=None, label="Statement covers unit and its children")
    currency = forms.RegexField(regex=r"^[A-Z]{3}$", max_length=3, initial="MUR",
                                help_text="One currency per statement; no exchange-rate conversion.")
    statement_date = forms.DateField(initial=timezone.localdate,
                                     widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    document = forms.FileField(help_text="CSV, XLS or XLSX, up to 10 MB. One row per pending invoice.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user, MANAGERS)

    def clean_statement_date(self):
        value = self.cleaned_data["statement_date"]
        if value > timezone.localdate():
            raise forms.ValidationError("A provider statement cannot be dated in the future.")
        return value


class AssignmentForm(forms.Form):
    org_unit = forms.ModelChoiceField(queryset=None, label="Responsible unit")
    assigned_to = forms.ModelChoiceField(queryset=User.objects.none(), required=False, label="Responsible staff member")
    reason = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, user, row, chosen_unit=None, **kwargs):
        super().__init__(*args, **kwargs)
        units = units_for(user, MANAGERS).filter(path__startswith=row.statement.org_unit.path)
        self.fields["org_unit"].queryset = units
        chosen = self.data.get("org_unit") if self.is_bound else chosen_unit or row.assigned_unit_id
        try:
            unit = units.filter(pk=chosen).first()
        except (ValueError, TypeError):
            unit = None
        if unit:
            self.fields["assigned_to"].queryset = users_with_role(unit, MANAGERS + [Role.CLERK])
        self.initial.update(org_unit=unit.pk if unit else row.assigned_unit_id, assigned_to=row.assigned_to_id)
