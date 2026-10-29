"""One-time production administrator enrollment, protected by a separate token."""

import hmac
import io

from django import forms
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.core.management import call_command
from django.db import connection, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from accounts import security
from accounts.middleware import OTP_SESSION_KEY
from accounts.models import User
from core.models import AuditEvent, ProductionBootstrap
from core.request_context import client_ip
from core.production_readiness import schema_ready


class InitialAdministratorForm(UserCreationForm):
    full_name = forms.CharField(max_length=150)
    setup_token = forms.CharField(label="One-time setup token", max_length=512, strip=False,
                                 widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ["username", "full_name", "email", "password1", "password2"]


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        ready = schema_ready()
        return JsonResponse({"status": "ok" if ready else "schema_not_ready"}, status=200 if ready else 503)
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)


@sensitive_post_parameters("setup_token", "password1", "password2")
@require_http_methods(["GET", "POST"])
def initialize(request):
    if not settings.PRODUCTION:
        return redirect("accounts:login")
    if User.objects.exists() or ProductionBootstrap.objects.exists():
        return redirect("accounts:login")
    expected = getattr(settings, "GOVBILLS_SETUP_TOKEN", "")
    if len(expected) < 32:
        return render(request, "accounts/production_setup.html", {"setup_unavailable": True}, status=503)
    form = InitialAdministratorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ip = client_ip(request)
        locked = security.is_locked_out("production-setup", ip)
        token_ok = hmac.compare_digest(form.cleaned_data["setup_token"].encode(), expected.encode())
        if locked or not token_ok:
            security.record_attempt("production-setup", ip, False)
            form.add_error("setup_token", "Setup authorization failed or is temporarily locked.")
        else:
            with transaction.atomic():
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [74062601])
                if User.objects.exists() or ProductionBootstrap.objects.exists():
                    return redirect("accounts:login")
                user = form.save(commit=False)
                user.is_superuser = user.is_staff = True
                user.save()
                call_command("setup_govbills", name="Government of Mauritius", code="GOM", stdout=io.StringIO())
                ProductionBootstrap.objects.create(singleton=1)
                AuditEvent.record("production.initial_admin_created", obj=user, actor=user)
            security.record_attempt("production-setup", ip, True)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            request.session[OTP_SESSION_KEY] = False
            return redirect("accounts:otp_enroll")
    return render(request, "accounts/production_setup.html", {"form": form})
