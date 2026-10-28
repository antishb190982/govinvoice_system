from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts import security
from accounts.forms import (LoginForm, MembershipForm, OtpForm, StrongPasswordChangeForm, UserCreateForm,
                            UserEditForm)
from accounts.middleware import OTP_SESSION_KEY
from accounts.models import RecoveryCode, User
from core.models import AuditEvent
from core.request_context import client_ip
from org.access import active_memberships, units_for, user_has_any_role
from org.models import Membership, Role

GENERIC_LOGIN_ERROR = "Invalid credentials, or the account is temporarily locked."


def _safe_next(request):
    nxt = request.GET.get("next") or request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, require_https=request.is_secure()):
        return nxt
    return None


def login_view(request):
    if request.user.is_authenticated and request.session.get(OTP_SESSION_KEY):
        return redirect("core:dashboard")
    form = LoginForm(request.POST or None)
    ip = client_ip(request)
    if request.method == "POST" and form.is_valid():
        username = form.cleaned_data["username"].strip()
        if security.is_locked_out(username, ip):
            security.record_attempt(username, ip, False)
            AuditEvent.record("auth.login_locked", data={"username": username}, ip_address=ip)
            form.add_error(None, GENERIC_LOGIN_ERROR)
        else:
            user = authenticate(request, username=username, password=form.cleaned_data["password"])
            if user is None:
                security.record_attempt(username, ip, False)
                AuditEvent.record("auth.login_failed", data={"username": username}, ip_address=ip)
                form.add_error(None, GENERIC_LOGIN_ERROR)
            else:
                security.record_attempt(username, ip, True)
                login(request, user)
                request.session[OTP_SESSION_KEY] = False
                if nxt := _safe_next(request):
                    request.session["after_otp"] = nxt
                return redirect("accounts:otp_verify" if user.has_two_factor else "accounts:otp_enroll")
    return render(request, "accounts/login.html", {"form": form})


@login_required
def otp_verify(request):
    user = request.user
    if not user.has_two_factor:
        return redirect("accounts:otp_enroll")
    form = OtpForm(request.POST or None)
    ip = client_ip(request)
    if request.method == "POST" and form.is_valid():
        if security.is_locked_out(user.username, ip):
            logout(request)
            messages.error(request, GENERIC_LOGIN_ERROR)
            return redirect("accounts:login")
        code = form.cleaned_data["code"]
        ok = RecoveryCode.consume(user, code) if form.cleaned_data["use_recovery"] else security.verify_totp(user, code)
        security.record_attempt(user.username, ip, ok, stage="otp")
        if ok:
            request.session.cycle_key()
            request.session[OTP_SESSION_KEY] = True
            AuditEvent.record("auth.login", obj=user, actor=user,
                              data={"method": "recovery" if form.cleaned_data["use_recovery"] else "totp"})
            if form.cleaned_data["use_recovery"]:
                remaining = RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
                messages.warning(request, f"Recovery code used. {remaining} remaining.")
            return redirect(request.session.pop("after_otp", None) or "core:dashboard")
        AuditEvent.record("auth.otp_failed", obj=user, actor=user)
        form.add_error("code", "That code is not valid.")
    return render(request, "accounts/otp_verify.html", {"form": form})


@login_required
def otp_enroll(request):
    user = request.user
    if user.has_two_factor:
        return redirect("accounts:otp_verify" if not request.session.get(OTP_SESSION_KEY) else "accounts:profile")
    secret = request.session.get("pending_totp_secret")
    if not secret:
        secret = security.new_totp_secret()
        request.session["pending_totp_secret"] = secret
    form = OtpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if security.verify_totp(user, form.cleaned_data["code"], secret=secret):
            user.totp_secret = secret
            user.totp_confirmed_at = timezone.now()
            user.save(update_fields=["totp_secret", "totp_confirmed_at"])
            del request.session["pending_totp_secret"]
            request.session.cycle_key()
            request.session[OTP_SESSION_KEY] = True
            codes = RecoveryCode.regenerate(user)
            AuditEvent.record("auth.2fa_enrolled", obj=user, actor=user)
            return render(request, "accounts/recovery_codes.html", {"codes": codes, "first_time": True})
        form.add_error("code", "That code is not valid. Check the time on your phone.")
    return render(request, "accounts/otp_enroll.html", {
        "form": form, "qr": security.provisioning_qr_data_uri(user, secret),
        "secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
    })


@login_required
def recovery_codes(request):
    form = OtpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if security.verify_totp(request.user, form.cleaned_data["code"]):
            codes = RecoveryCode.regenerate(request.user)
            AuditEvent.record("auth.recovery_codes_regenerated", obj=request.user)
            return render(request, "accounts/recovery_codes.html", {"codes": codes})
        form.add_error("code", "That code is not valid.")
    return render(request, "accounts/recovery_regenerate.html", {"form": form})


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        AuditEvent.record("auth.logout", obj=request.user)
    logout(request)
    return redirect("accounts:login")


@login_required
def password_change(request):
    form = StrongPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.must_change_password = False
        user.save(update_fields=["must_change_password", "password_changed_at"])
        update_session_auth_hash(request, user)
        AuditEvent.record("auth.password_changed", obj=user)
        messages.success(request, "Your password has been changed.")
        return redirect("core:dashboard")
    return render(request, "accounts/password_change.html", {"form": form})


@login_required
def profile(request):
    return render(request, "accounts/profile.html", {
        "memberships": active_memberships(request.user),
        "recovery_left": RecoveryCode.objects.filter(user=request.user, used_at__isnull=True).count(),
    })


# --- User administration ------------------------------------------------------

def _require_user_admin(request):
    if not user_has_any_role(request.user, [Role.UNIT_ADMIN]):
        raise PermissionDenied


def _manageable_users(admin):
    if admin.is_superuser:
        return User.objects.all()
    units = units_for(admin, [Role.UNIT_ADMIN])
    # Users who belong somewhere in the admin's subtree, plus users with no
    # role yet (just created); never system administrators.
    return User.objects.filter(Q(memberships__org_unit__in=units) | Q(memberships__isnull=True)) \
        .exclude(is_superuser=True).distinct()


@login_required
def user_list(request):
    _require_user_admin(request)
    q = request.GET.get("q", "").strip()
    users = _manageable_users(request.user).prefetch_related("memberships__org_unit")
    if q:
        users = users.filter(Q(full_name__icontains=q) | Q(username__icontains=q) | Q(email__icontains=q))
    return render(request, "accounts/user_list.html", {"users": users[:300], "q": q})


@login_required
def user_create(request):
    _require_user_admin(request)
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        AuditEvent.record("user.created", obj=user, data={"username": user.username})
        messages.success(request, f"{user} created. Now grant them a role.")
        return redirect("accounts:user_detail", pk=user.pk)
    return render(request, "accounts/user_form.html", {"form": form, "title": "New user"})


@login_required
def user_detail(request, pk):
    _require_user_admin(request)
    target = get_object_or_404(_manageable_users(request.user), pk=pk)
    allowed_units = units_for(request.user, [Role.UNIT_ADMIN])
    edit_form = UserEditForm(request.POST if request.POST.get("form") == "edit" else None, instance=target)
    role_form = MembershipForm(request.POST if request.POST.get("form") == "role" else None,
                               allowed_units=allowed_units)
    if request.method == "POST":
        action = request.POST.get("form")
        if action == "edit" and edit_form.is_valid():
            changed = edit_form.changed_data
            edit_form.save()
            AuditEvent.record("user.updated", obj=target, data={"fields": changed})
            messages.success(request, "User updated.")
            return redirect("accounts:user_detail", pk=pk)
        if action == "role" and role_form.is_valid():
            membership, created = Membership.objects.get_or_create(
                user=target, org_unit=role_form.cleaned_data["org_unit"], role=role_form.cleaned_data["role"],
                defaults={"valid_until": role_form.cleaned_data["valid_until"]})
            if created:
                AuditEvent.record("user.role_granted", obj=target, org_unit_id=membership.org_unit_id,
                                  data={"role": membership.role, "unit": membership.org_unit.code,
                                        "valid_until": membership.valid_until})
                messages.success(request, "Role granted.")
            return redirect("accounts:user_detail", pk=pk)
        if action == "revoke":
            membership = get_object_or_404(Membership, pk=request.POST.get("membership"), user=target,
                                           org_unit__in=allowed_units)
            AuditEvent.record("user.role_revoked", obj=target, org_unit_id=membership.org_unit_id,
                              data={"role": membership.role, "unit": membership.org_unit.code})
            membership.delete()
            messages.success(request, "Role revoked.")
            return redirect("accounts:user_detail", pk=pk)
        if action == "reset_2fa":
            if target == request.user:
                raise PermissionDenied
            target.totp_secret = ""
            target.totp_confirmed_at = None
            target.save(update_fields=["totp_secret", "totp_confirmed_at"])
            RecoveryCode.objects.filter(user=target).delete()
            AuditEvent.record("user.2fa_reset", obj=target)
            messages.success(request, "Two-factor login reset; the user will enrol again at next sign-in.")
            return redirect("accounts:user_detail", pk=pk)
    return render(request, "accounts/user_detail.html", {
        "target": target, "edit_form": edit_form, "role_form": role_form,
        "memberships": target.memberships.select_related("org_unit"),
        "allowed_unit_ids": set(allowed_units.values_list("pk", flat=True)),
    })
