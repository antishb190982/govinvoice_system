"""Administrator password changes never expose or record password material."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from accounts.models import User
from accounts import security
from accounts.views import _manageable_users, _require_user_admin
from core.models import AuditEvent
from core.request_context import client_ip
from org.access import units_for
from org.models import Role


def may_change_password(admin, target):
    if target.pk == admin.pk:
        return False  # Use the existing current-password self-service flow.
    if admin.is_superuser:
        return True
    if target.is_superuser or target.is_staff:
        return False
    allowed = units_for(admin, [Role.UNIT_ADMIN])
    return (target.memberships.filter(org_unit__in=allowed).exists()
        and not target.memberships.exclude(org_unit__in=allowed).exists())


class AdminPasswordForm(SetPasswordForm):
    mode = forms.ChoiceField(choices=[("reset", "Reset with a temporary password"),
                                     ("set", "Set a password")], initial="reset")
    administrator_password = forms.CharField(label="Your administrator password",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    require_change = forms.BooleanField(required=False, initial=True,
        label="Require a password change at next sign-in",
        help_text="Always required for a temporary reset.")
    confirm = forms.BooleanField(label="I understand this signs the user out of existing sessions.")

    def __init__(self, *args, administrator, **kwargs):
        self.administrator = administrator
        super().__init__(*args, **kwargs)
        self.order_fields(["mode", "new_password1", "new_password2", "require_change",
                           "administrator_password", "confirm"])

    def clean_administrator_password(self):
        value = self.cleaned_data["administrator_password"]
        if not self.administrator.check_password(value):
            raise forms.ValidationError("Your administrator password is incorrect.")
        return value

    def clean_new_password1(self):
        value = self.cleaned_data.get("new_password1")
        if value and self.user.check_password(value):
            raise forms.ValidationError("Choose a password different from the current one.")
        return value


@sensitive_post_parameters()
@never_cache
@login_required
def password_admin(request, pk):
    _require_user_admin(request)
    target = get_object_or_404(_manageable_users(request.user), pk=pk)
    if not may_change_password(request.user, target):
        raise PermissionDenied("Use self-service for your own password. System administrators handle staff and cross-unit accounts.")
    form = AdminPasswordForm(target, data=request.POST if request.method == "POST" else None,
        administrator=request.user, initial={"mode": request.GET.get("mode", "reset")})
    if request.method == "POST":
        if security.is_locked_out(request.user.username, client_ip(request)):
            form.add_error(None, "Credential changes are temporarily locked. Try again later.")
            return render(request, "accounts/password_admin.html", {"target": target, "form": form}, status=429)
        if not request.user.check_password(request.POST.get("administrator_password", "")):
            security.record_attempt(request.user.username, client_ip(request), False)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            target = User.objects.select_for_update().get(pk=pk)
            if not may_change_password(request.user, target):
                raise PermissionDenied
            target.set_password(form.cleaned_data["new_password1"])
            target.must_change_password = form.cleaned_data["mode"] == "reset" or form.cleaned_data["require_change"]
            target.save(update_fields=["password", "password_changed_at", "must_change_password"])
            AuditEvent.record("user.password_reset" if form.cleaned_data["mode"] == "reset" else "user.password_set",
                obj=target, actor=request.user, data={"require_change": target.must_change_password})
        messages.success(request, "Password updated. Existing sessions are invalidated; two-factor verification and the signing PIN are unchanged.")
        return redirect("accounts:user_detail", pk=pk)
    return render(request, "accounts/password_admin.html", {"target": target, "form": form})
