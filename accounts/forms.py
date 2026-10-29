from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import PasswordChangeForm

from accounts.models import User
from org.models import Membership, OrgUnit, Role


class LoginForm(forms.Form):
    username = forms.CharField(max_length=150, widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class OtpForm(forms.Form):
    code = forms.CharField(
        label="Authentication code", max_length=12,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}),
    )
    use_recovery = forms.BooleanField(required=False, label="This is a recovery code")


class StrongPasswordChangeForm(PasswordChangeForm):
    def clean_new_password1(self):
        new = self.cleaned_data.get("new_password1")
        if new and self.user.check_password(new):
            raise forms.ValidationError("Choose a password different from the current one.")
        return new


class UserCreateForm(forms.ModelForm):
    temporary_password = forms.CharField(widget=forms.PasswordInput, help_text="The user must change it on first sign-in.")
    initial_unit = forms.ModelChoiceField(queryset=OrgUnit.objects.none(), label="Initial unit")
    initial_role = forms.ChoiceField(choices=Role.choices, initial=Role.VIEWER, label="Initial role")

    class Meta:
        model = User
        fields = ["username", "full_name", "email", "job_title", "employee_id", "phone"]

    def __init__(self, *args, allowed_units=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["initial_unit"].queryset = allowed_units if allowed_units is not None else OrgUnit.objects.none()
        self.fields["initial_unit"].label_from_instance = lambda unit: unit.indented_name

    def clean_temporary_password(self):
        pw = self.cleaned_data["temporary_password"]
        password_validation.validate_password(pw)
        return pw

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["temporary_password"])
        user.must_change_password = True
        if commit:
            user.save()
        return user


class UserEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["full_name", "email", "job_title", "employee_id", "phone", "is_active"]


class MembershipForm(forms.ModelForm):
    class Meta:
        model = Membership
        fields = ["org_unit", "role", "valid_until"]
        widgets = {"valid_until": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, allowed_units=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = allowed_units if allowed_units is not None else OrgUnit.objects.all()
        self.fields["org_unit"].label_from_instance = lambda u: u.indented_name + f" ({u.code})"
