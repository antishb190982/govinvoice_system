"""
Runtime configuration managed from the admin console.

Each editable setting is declared once in REGISTRY. Values saved in the
console are stored in ConfigEntry and overlaid onto django.conf.settings at the
start of every request (and every scheduler loop), so the rest of the code keeps
reading plain settings. The environment value remains the default.
"""

import ipaddress

from django.conf import settings
from django.core.exceptions import ValidationError

# key: (group, label, type, help). Types: int, float, str, bool, secret, choice:<a|b>, days, email, url
REGISTRY = {
    # Business rules
    "SETTLEMENT_LEAD_DAYS": ("Business rules", "Settlement lead time (working days)", "int",
                             "Approvals must finish this many working days before an invoice's due date."),
    "DUE_SOON_DAYS": ("Business rules", "“Due soon” window (days)", "int", "Dashboards and alerts look this far ahead."),
    "ANOMALY_THRESHOLD_PCT": ("Business rules", "Spike threshold (%)", "int",
                              "Flag invoices this much above the account's recent average."),
    "DEFAULT_CURRENCY": ("Business rules", "Default currency", "str", "ISO code, e.g. MUR."),
    "MAX_UPLOAD_MB": ("Business rules", "Maximum upload size (MB)", "int", ""),
    # Working calendar
    "WORK_WEEK_DAYS": ("Working calendar", "Working days", "days", "Days on which SLA clocks run."),
    "WORK_DAY_START_HOUR": ("Working calendar", "Working day starts (hour)", "int", "0–23"),
    "WORK_DAY_END_HOUR": ("Working calendar", "Working day ends (hour)", "int", "1–23"),
    # Payments
    "PAYER_NAME": ("Payments", "Paying entity name", "str", "Shown on settlement instructions and bank files."),
    "PAYER_IBAN": ("Payments", "Paying account IBAN", "secret", "Debited by ISO 20022 payment files."),
    "PAYER_BIC": ("Payments", "Paying bank BIC", "str", ""),
    # OCR
    "OCR_LANGUAGES": ("OCR", "OCR languages", "str", "Tesseract codes joined by +, e.g. eng+fra."),
    "OCR_REVIEW_THRESHOLD": ("OCR", "Confirmation threshold (0–1)", "float",
                             "Required fields read with lower confidence must be confirmed by a person."),
    "OCR_SYNC_MAX_FILES": ("OCR", "Analyse immediately up to (files)", "int", "Larger uploads go to the background worker."),
    # Security
    "LOGIN_MAX_FAILURES": ("Security", "Failed sign-ins before lockout", "int", ""),
    "LOGIN_LOCKOUT_MINUTES": ("Security", "Lockout duration (minutes)", "int", ""),
    "SESSION_IDLE_TIMEOUT_MINUTES": ("Security", "Idle session timeout (minutes)", "int", ""),
    "SIGNING_PIN_MAX_FAILURES": ("Security", "Wrong signing PINs before lock", "int", ""),
    # Branding
    "BRAND_NAME": ("Branding", "Product name", "str", ""),
    "BRAND_HIGHLIGHT": ("Branding", "Highlighted end of the name", "str", "Shown in the accent colour."),
    "BRAND_TAGLINE": ("Branding", "Tagline", "str", ""),
    "SITE_URL": ("Branding", "Public URL", "url", "Used in e-mail and SMS links."),
    # E-mail (SMTP)
    "SMTP_ENABLED": ("E-mail (SMTP)", "Send e-mail", "bool", ""),
    "SMTP_HOST": ("E-mail (SMTP)", "SMTP host", "str", ""),
    "SMTP_PORT": ("E-mail (SMTP)", "SMTP port", "int", "587 for STARTTLS, 465 for SSL."),
    "SMTP_SECURITY": ("E-mail (SMTP)", "Connection security", "choice:starttls|ssl|none", ""),
    "SMTP_USERNAME": ("E-mail (SMTP)", "Username", "str", ""),
    "SMTP_PASSWORD": ("E-mail (SMTP)", "Password", "secret", ""),
    "SMTP_FROM": ("E-mail (SMTP)", "From address", "email", ""),
    "SMTP_TIMEOUT": ("E-mail (SMTP)", "Timeout (seconds)", "int", ""),
    # SMS
    "SMS_ENABLED": ("SMS", "Send SMS", "bool", "Critical alerts (SLA breaches, payment deadlines) go to users' mobile numbers."),
    "SMS_PROVIDER": ("SMS", "Gateway type", "choice:http|twilio", "Generic HTTP/JSON gateway (local operators) or Twilio."),
    "SMS_URL": ("SMS", "Gateway URL", "url", "HTTP gateway endpoint, or Twilio API base."),
    "SMS_ACCOUNT": ("SMS", "Account / sender ID", "str", "Twilio account SID, or the gateway's account name."),
    "SMS_TOKEN": ("SMS", "API token / password", "secret", ""),
    "SMS_SENDER": ("SMS", "Sender name or number", "str", "e.g. GOVMU or +230…"),
    "SMS_BODY_TEMPLATE": ("SMS", "HTTP body template (JSON)", "str",
                          'Placeholders {to}, {text}, {sender}. e.g. {"to":"{to}","message":"{text}","from":"{sender}"}'),
    "SMS_MIN_LEVEL": ("SMS", "Send SMS for", "choice:critical|warning|info", "Lowest alert level sent by SMS."),
    "SMS_COUNTRY_CODE": ("SMS", "Default country code", "str", "Prefixed to local numbers, e.g. +230."),
    # Integrations
    "API_ENABLED": ("Integrations", "REST API enabled", "bool", ""),
    "MCP_ENABLED": ("Integrations", "MCP server enabled", "bool", "Lets approved AI assistants read data through MCP."),
}

DEFAULTS = {
    "SMTP_ENABLED": False, "SMTP_HOST": "", "SMTP_PORT": 587, "SMTP_SECURITY": "starttls", "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "", "SMTP_FROM": "", "SMTP_TIMEOUT": 15,
    "SMS_ENABLED": False, "SMS_PROVIDER": "http", "SMS_URL": "", "SMS_ACCOUNT": "", "SMS_TOKEN": "", "SMS_SENDER": "",
    "SMS_BODY_TEMPLATE": '{"to": "{to}", "message": "{text}", "from": "{sender}"}', "SMS_MIN_LEVEL": "critical",
    "SMS_COUNTRY_CODE": "+230", "API_ENABLED": True, "MCP_ENABLED": True,
}
_ENV_DEFAULTS = {}


def groups():
    out = {}
    for key, (group, label, kind, help_text) in REGISTRY.items():
        out.setdefault(group, []).append(key)
    return out


def default(key):
    if key not in _ENV_DEFAULTS:
        _ENV_DEFAULTS[key] = getattr(settings, key, DEFAULTS.get(key))
    return _ENV_DEFAULTS[key]


def get(key):
    return getattr(settings, key, DEFAULTS.get(key))


def parse(key, raw):
    """Validate a console value for `key`; returns the typed value."""
    kind = REGISTRY[key][2]
    if kind == "bool":
        return raw in (True, "on", "true", "1", 1)
    if kind == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValidationError(f"{REGISTRY[key][1]}: enter a whole number.")
        if value < 0:
            raise ValidationError(f"{REGISTRY[key][1]}: must not be negative.")
        return value
    if kind == "float":
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValidationError(f"{REGISTRY[key][1]}: enter a number.")
        if not 0 <= value <= 1:
            raise ValidationError(f"{REGISTRY[key][1]}: must be between 0 and 1.")
        return value
    if kind == "days":
        values = sorted({int(v) for v in (raw if isinstance(raw, list) else [raw]) if str(v).isdigit() and 0 <= int(v) <= 6})
        if not values:
            raise ValidationError("Choose at least one working day.")
        return values
    if kind.startswith("choice:"):
        options = kind.split(":", 1)[1].split("|")
        if raw not in options:
            raise ValidationError(f"{REGISTRY[key][1]}: choose one of {', '.join(options)}.")
        return raw
    value = str(raw or "").strip()
    if kind == "email" and value and "@" not in value:
        raise ValidationError(f"{REGISTRY[key][1]}: enter an e-mail address.")
    if kind == "url" and value and not value.startswith(("https://", "http://")):
        raise ValidationError(f"{REGISTRY[key][1]}: enter a full URL.")
    return value


def validate_combination(values):
    start, end = values.get("WORK_DAY_START_HOUR", get("WORK_DAY_START_HOUR")), values.get("WORK_DAY_END_HOUR", get("WORK_DAY_END_HOUR"))
    if not (0 <= start < end <= 23):
        raise ValidationError("The working day must start before it ends (hours 0–23).")


def save(values, user):
    """Persist console values (secrets only when a new one is typed) and bump the version."""
    from core.models import AuditEvent
    from integrations.models import ConfigEntry

    validate_combination(values)
    changed = []
    for key, value in values.items():
        kind = REGISTRY[key][2]
        entry, _ = ConfigEntry.objects.get_or_create(key=key)
        if kind == "secret":
            if value in (None, ""):
                continue  # blank = keep the stored secret
            entry.secret_value, entry.value = value, None
        else:
            if entry.value == value:
                continue
            entry.value = value
        entry.updated_by = user
        entry.save()
        changed.append(key)
    if changed:
        AuditEvent.record("config.changed", actor=user, data={"keys": changed})  # values not logged (may be secret)
        refresh(force=True)
    return changed


_applied = {"stamp": None, "checked": 0.0, "overridden": set()}
CHECK_EVERY_SECONDS = 10


def refresh(force=False):
    """Overlay stored values onto settings. Each process re-checks the database
    change-stamp at most every few seconds, so all workers converge quickly."""
    import time

    from django.db import DatabaseError
    from django.db.models import Count, Max

    from integrations.models import ConfigEntry

    now = time.monotonic()
    if not force and now - _applied["checked"] < CHECK_EVERY_SECONDS:
        return
    _applied["checked"] = now
    try:
        agg = ConfigEntry.objects.aggregate(m=Max("updated_at"), n=Count("id"))
        stamp = (agg["m"], agg["n"])
        if not force and stamp == _applied["stamp"]:
            return
        entries = list(ConfigEntry.objects.all())
    except DatabaseError:
        return  # before migrations
    applied = set()
    for entry in entries:
        if entry.key not in REGISTRY:
            continue
        value = entry.secret_value if REGISTRY[entry.key][2] == "secret" else entry.value
        if value is None or value == "" and REGISTRY[entry.key][2] == "secret":
            continue
        default(entry.key)  # remember the environment default before overriding
        setattr(settings, entry.key, value)
        applied.add(entry.key)
    for key in _applied["overridden"] - applied:
        setattr(settings, key, default(key))  # an override was removed: back to the environment value
    _applied["overridden"] = applied
    _applied["stamp"] = stamp


def ip_allowed(ip, allowed_text):
    rules = [line.strip() for line in (allowed_text or "").splitlines() if line.strip()]
    if not rules:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for rule in rules:
        try:
            if addr in ipaddress.ip_network(rule, strict=False):
                return True
        except ValueError:
            continue
    return False
