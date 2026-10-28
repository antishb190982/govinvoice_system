"""Admin console: system health, runtime settings and integrations. System administrators only."""

import os
import shutil
from datetime import date, timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import connection
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from accounts.models import User
from core.models import AuditEvent, Holiday
from core.utils import paginate
from integrations import config, sms
from integrations.models import ApiClient, ApiRequestLog, ConfigEntry, OutboundMessage
from org.models import OrgUnit

DAYS = [(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun")]
HEARTBEAT_KEY = "_heartbeat_scheduler"


def sysadmin_required(view):
    @login_required
    def inner(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    inner.__name__ = view.__name__
    return inner


# --- Health ---------------------------------------------------------------------------

def database_status():
    info = {"vendor": connection.vendor, "name": str(connection.settings_dict.get("NAME")), "ok": False}
    try:
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute("SELECT version(), pg_database_size(current_database())")
                version, size = cur.fetchone()
                info.update(version=version.split(",")[0], size=size)
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
                info["connections"] = cur.fetchone()[0]
            elif connection.vendor == "sqlite":
                cur.execute("SELECT sqlite_version()")
                info["version"] = "SQLite " + cur.fetchone()[0]
                path = connection.settings_dict.get("NAME")
                info["size"] = os.path.getsize(path) if path and os.path.exists(str(path)) else None
                info["warning"] = "SQLite is for development only; use PostgreSQL in production."
            else:
                info["version"] = connection.vendor
        info["ok"] = True
    except Exception as exc:  # report, never crash the console
        info["error"] = f"{exc.__class__.__name__}: {exc}"
    try:
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        info["pending_migrations"] = len(executor.migration_plan(executor.loader.graph.leaf_nodes()))
    except Exception:
        info["pending_migrations"] = None
    return info


def _dir_size(path, limit=200000):
    total, count = 0, 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            count += 1
            if count > limit:
                return total, count, True
    return total, count, False


def health():
    from imports.models import ImportRow
    from invoices.models import Invoice
    from ocr.engine import engine_version
    from signatures import ca

    try:
        os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
        storage_bytes, storage_files, truncated = _dir_size(settings.MEDIA_ROOT)
        disk = shutil.disk_usage(settings.MEDIA_ROOT)
        storage = {"bytes": storage_bytes, "files": storage_files, "truncated": truncated,
                   "free": disk.free, "total": disk.total, "free_pct": round(100 * disk.free / disk.total)}
    except OSError as exc:
        storage = {"error": str(exc), "free_pct": 0, "bytes": 0, "files": 0, "free": 0, "total": 0}
    try:
        import pytesseract

        languages = sorted(set(pytesseract.get_languages(config="")) - {"osd"})
    except Exception:
        languages = []
    try:
        cert = ca.ca_certificate()
        ca_info = {"subject": cert.subject.rfc4514_string(), "expires": cert.not_valid_after_utc,
                   "days_left": (cert.not_valid_after_utc - timezone.now()).days}
    except Exception as exc:
        ca_info = {"error": str(exc)}
    beat = ConfigEntry.objects.filter(key=HEARTBEAT_KEY).first()
    beat_at = beat.updated_at if beat else None
    day = timezone.now() - timedelta(days=1)
    return {
        "db": database_status(),
        "storage": storage,
        "ocr": {"engine": engine_version(), "languages": languages, "configured": settings.OCR_LANGUAGES,
                "queue": ImportRow.objects.filter(status=ImportRow.Status.ANALYSING).count()},
        "scheduler": {"last": beat_at, "ok": bool(beat_at and timezone.now() - beat_at < timedelta(minutes=30))},
        "ca": ca_info,
        "email": {"enabled": config.get("SMTP_ENABLED"), "host": config.get("SMTP_HOST"),
                  **OutboundMessage.objects.filter(channel="email", created_at__gte=day).aggregate(
                      sent=Count("id", filter=Q(status="sent")), failed=Count("id", filter=Q(status="failed")))},
        "sms": {"enabled": config.get("SMS_ENABLED"), "provider": config.get("SMS_PROVIDER"),
                **OutboundMessage.objects.filter(channel="sms", created_at__gte=day).aggregate(
                    sent=Count("id", filter=Q(status="sent")), failed=Count("id", filter=Q(status="failed")))},
        "api": {"enabled": config.get("API_ENABLED"), "mcp": config.get("MCP_ENABLED"),
                "clients": ApiClient.objects.filter(is_active=True, revoked_at__isnull=True).count(),
                **ApiRequestLog.objects.filter(created_at__gte=day).aggregate(
                    requests=Count("id"), errors=Count("id", filter=Q(status__gte=400)))},
        "counts": {"invoices": Invoice.objects.count(), "users": User.objects.filter(is_active=True).count(),
                   "audit": AuditEvent.objects.count()},
        "env": {"environment": settings.ENVIRONMENT, "debug": settings.DEBUG, "timezone": settings.TIME_ZONE,
                "currency": settings.DEFAULT_CURRENCY},
    }


@sysadmin_required
def overview(request):
    chain = None
    if request.GET.get("verify"):
        ok, bad, count = AuditEvent.verify_chain()
        chain = {"ok": ok, "bad": bad, "count": count}
    return render(request, "integrations/overview.html", {"h": health(), "chain": chain, "tab": "overview"})


# --- Settings -------------------------------------------------------------------------------

def _settings_form(keys, data=None):
    class SettingsForm(forms.Form):
        pass

    form = SettingsForm(data)
    for key in keys:
        group, label, kind, help_text = config.REGISTRY[key]
        current = config.get(key)
        if kind == "bool":
            field = forms.BooleanField(required=False, initial=bool(current))
        elif kind == "secret":
            stored = ConfigEntry.objects.filter(key=key).exclude(secret_value="").exists() or bool(current)
            field = forms.CharField(required=False, widget=forms.PasswordInput(
                attrs={"autocomplete": "new-password", "placeholder": "•••••• stored – leave blank to keep" if stored else ""}))
        elif kind == "days":
            field = forms.MultipleChoiceField(choices=DAYS, widget=forms.CheckboxSelectMultiple, initial=current, required=False)
        elif kind.startswith("choice:"):
            field = forms.ChoiceField(choices=[(o, o) for o in kind.split(":", 1)[1].split("|")], initial=current)
        else:
            field = forms.CharField(required=False, initial="" if current is None else current)
        field.label, field.help_text = label, help_text
        form.fields[key] = field
    return form


def _save_form(request, keys):
    form = _settings_form(keys, request.POST)
    if not form.is_valid():
        return form, False
    values, problems = {}, []
    for key in keys:
        raw = form.cleaned_data.get(key)
        if config.REGISTRY[key][2] == "days":
            raw = [int(v) for v in raw or []]
        try:
            values[key] = config.parse(key, raw)
        except ValidationError as exc:
            problems += exc.messages
    if problems:
        for p in problems:
            form.add_error(None, p)
        return form, False
    try:
        changed = config.save(values, request.user)
    except ValidationError as exc:
        form.add_error(None, exc)
        return form, False
    messages.success(request, f"Saved ({len(changed)} change{'s' if len(changed) != 1 else ''}).")
    return form, True


@sysadmin_required
def settings_view(request, group=None):
    groups = {g: keys for g, keys in config.groups().items() if g not in ("E-mail (SMTP)", "SMS", "Integrations")}
    slugs = {slugify(g): g for g in groups}
    current = slugs.get(group) or next(iter(groups))
    keys = groups[current]
    if request.method == "POST":
        form, ok = _save_form(request, keys)
        if ok:
            return redirect("console:settings_group", group=slugify(current))
    else:
        form = _settings_form(keys)
    overridden = set(ConfigEntry.objects.filter(key__in=keys).values_list("key", flat=True))
    return render(request, "integrations/settings.html", {
        "form": form, "groups": [(slugify(g), g) for g in groups], "current": slugify(current), "title": current,
        "overridden": overridden, "tab": "settings"})


class TestForm(forms.Form):
    to = forms.CharField(label="Send a test to", max_length=254)


@sysadmin_required
def email_view(request):
    keys = config.groups()["E-mail (SMTP)"]
    form, test = _settings_form(keys), TestForm(initial={"to": request.user.email})
    if request.method == "POST" and request.POST.get("action") == "save":
        form, ok = _save_form(request, keys)
        if ok:
            return redirect("console:email")
    elif request.method == "POST" and request.POST.get("action") == "test":
        test = TestForm(request.POST)
        if test.is_valid():
            try:
                send_mail(f"[{settings.BRAND_NAME}] Test e-mail", "SMTP is configured correctly.", None,
                          [test.cleaned_data["to"]], fail_silently=False)
                last = OutboundMessage.objects.filter(channel="email").first()
                if last and last.status == "skipped":
                    messages.warning(request, "SMTP is disabled: the message was written to the server log instead.")
                else:
                    messages.success(request, "Test e-mail sent.")
            except Exception as exc:
                messages.error(request, f"Sending failed: {exc.__class__.__name__}: {exc}")
            AuditEvent.record("config.email_test", data={"to": test.cleaned_data["to"]})
            return redirect("console:email")
    return render(request, "integrations/channel.html", {
        "form": form, "test": test, "tab": "email", "title": "E-mail (SMTP)",
        "intro": "Outbound e-mail for alerts, approvals and payment deadlines. Passwords are stored encrypted.",
        "log": OutboundMessage.objects.filter(channel="email")[:15]})


@sysadmin_required
def sms_view(request):
    keys = config.groups()["SMS"]
    form, test = _settings_form(keys), TestForm(initial={"to": request.user.phone})
    if request.method == "POST" and request.POST.get("action") == "save":
        form, ok = _save_form(request, keys)
        if ok:
            return redirect("console:sms")
    elif request.method == "POST" and request.POST.get("action") == "test":
        test = TestForm(request.POST)
        if test.is_valid():
            number = sms.normalise_number(test.cleaned_data["to"])
            if not number:
                messages.error(request, "That is not a valid phone number.")
            elif not config.get("SMS_ENABLED"):
                messages.warning(request, "Enable SMS and save first.")
            else:
                try:
                    ref = sms.send_raw(number, f"{settings.BRAND_NAME}: test message.")
                    OutboundMessage.objects.create(channel="sms", recipient=sms.mask(number), subject="Test",
                                                   status="sent", provider_reference=str(ref)[:120])
                    messages.success(request, "Test SMS accepted by the gateway.")
                except sms.SmsError as exc:
                    OutboundMessage.objects.create(channel="sms", recipient=sms.mask(number), subject="Test",
                                                   status="failed", error=str(exc))
                    messages.error(request, str(exc))
            AuditEvent.record("config.sms_test", data={"to": sms.mask(number) if number else ""})
            return redirect("console:sms")
    return render(request, "integrations/channel.html", {
        "form": form, "test": test, "tab": "sms", "title": "SMS gateway",
        "intro": "Critical alerts (SLA breaches, payment deadlines) are sent to users' mobile numbers. Works with a "
                 "local operator's HTTP/JSON SMS API or Twilio. Tokens are stored encrypted; numbers are masked in logs.",
        "log": OutboundMessage.objects.filter(channel="sms")[:15]})


# --- API clients -------------------------------------------------------------------------------

class ApiClientForm(forms.ModelForm):
    scopes = forms.MultipleChoiceField(choices=ApiClient.SCOPES, widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = ApiClient
        fields = ["name", "description", "scopes", "org_unit", "allowed_ips", "rate_limit_per_minute", "expires_at"]
        widgets = {"allowed_ips": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. 10.20.0.0/16"}),
                   "expires_at": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = OrgUnit.objects.all()
        self.fields["org_unit"].label_from_instance = lambda u: u.indented_name
        self.fields["rate_limit_per_minute"].initial = settings.API_DEFAULT_RATE_PER_MINUTE
        self.fields["expires_at"].initial = date.today() + timedelta(days=365)

    def clean_allowed_ips(self):
        import ipaddress

        text = self.cleaned_data["allowed_ips"]
        for line in text.splitlines():
            if line.strip():
                try:
                    ipaddress.ip_network(line.strip(), strict=False)
                except ValueError:
                    raise ValidationError(f"“{line.strip()}” is not an IP address or CIDR range.")
        return text


@sysadmin_required
def api_clients(request):
    form = ApiClientForm(request.POST or None)
    token = None
    if request.method == "POST" and form.is_valid():
        client = form.save(commit=False)
        username = f"api:{slugify(client.name)[:40]}"
        service_user, _ = User.objects.get_or_create(username=username, defaults={
            "full_name": f"API client – {client.name}", "email": f"{slugify(client.name)}@api.invalid", "is_active": False})
        client.service_user, client.created_by = service_user, request.user
        token = client.issue_token()
        client.save()
        AuditEvent.record("api.client_created", obj=client, data={"name": client.name, "scopes": client.scopes,
                                                                   "unit": client.org_unit.code if client.org_unit else None})
        form = ApiClientForm()
    clients = ApiClient.objects.select_related("org_unit").annotate(
        calls=Count("requests", filter=Q(requests__created_at__gte=timezone.now() - timedelta(days=1))))
    return render(request, "integrations/api_clients.html", {
        "form": form, "clients": clients, "token": token, "tab": "api",
        "base_url": settings.SITE_URL.rstrip("/"), "scopes": dict(ApiClient.SCOPES)})


@sysadmin_required
@require_POST
def api_client_action(request, pk, action):
    client = get_object_or_404(ApiClient, pk=pk)
    token = None
    if action == "revoke":
        client.revoked_at, client.is_active = timezone.now(), False
        client.save(update_fields=["revoked_at", "is_active"])
        AuditEvent.record("api.client_revoked", obj=client)
        messages.success(request, f"{client.name} revoked.")
    elif action == "rotate":
        token = client.issue_token()
        client.revoked_at, client.is_active = None, True
        client.save()
        AuditEvent.record("api.client_rotated", obj=client)
    if token:
        clients = ApiClient.objects.select_related("org_unit")
        return render(request, "integrations/api_clients.html", {
            "form": ApiClientForm(), "clients": clients, "token": token, "tab": "api",
            "base_url": settings.SITE_URL.rstrip("/"), "scopes": dict(ApiClient.SCOPES)})
    return redirect("console:api")


@sysadmin_required
def logs(request):
    kind = request.GET.get("kind", "api")
    if kind == "messages":
        page = paginate(request, OutboundMessage.objects.all(), 50)
    else:
        page = paginate(request, ApiRequestLog.objects.select_related("client"), 50)
    return render(request, "integrations/logs.html", {"page": page, "kind": kind, "tab": "logs"})


class HolidayForm(forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ["date", "name"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}


@sysadmin_required
def holidays(request):
    form = HolidayForm(request.POST or None)
    if request.method == "POST":
        if request.POST.get("delete"):
            h = get_object_or_404(Holiday, pk=request.POST["delete"])
            AuditEvent.record("config.holiday_removed", data={"date": h.date, "name": h.name})
            h.delete()
            return redirect("console:holidays")
        if form.is_valid():
            h = form.save()
            AuditEvent.record("config.holiday_added", data={"date": h.date, "name": h.name})
            messages.success(request, "Holiday added; SLA clocks skip it.")
            return redirect("console:holidays")
    upcoming = Holiday.objects.filter(date__gte=timezone.localdate() - timedelta(days=60))
    return render(request, "integrations/holidays.html", {"form": form, "holidays": upcoming, "tab": "holidays"})
