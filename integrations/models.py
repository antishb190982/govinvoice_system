import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.fields import EncryptedTextField
from org.models import OrgUnit


class ConfigEntry(models.Model):
    """A runtime override of a setting, edited in the admin console. Secrets are encrypted."""

    key = models.CharField(max_length=64, unique=True)
    value = models.JSONField(null=True, blank=True)
    secret_value = EncryptedTextField(blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class ApiClient(models.Model):
    """A system allowed to call the REST API or the MCP server with a bearer token."""

    SCOPES = [
        ("invoices:read", "Read invoices"),
        ("invoices:write", "Submit invoices (provider e-invoicing)"),
        ("providers:read", "Read providers and accounts"),
        ("settlements:read", "Read settlement instructions"),
        ("analytics:read", "Read spend analytics"),
        ("mcp", "Use the MCP server (AI assistants)"),
    ]
    TOKEN_PREFIX = "gbk_"

    name = models.CharField(max_length=120, unique=True)
    description = models.CharField(max_length=300, blank=True)
    token_id = models.CharField(max_length=16, unique=True, editable=False)
    token_hash = models.CharField(max_length=64, editable=False)
    scopes = models.JSONField(default=list)
    org_unit = models.ForeignKey(OrgUnit, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                 help_text="Restrict data to this unit and below; empty = whole government")
    allowed_ips = models.TextField(blank=True, help_text="One IP or CIDR range per line; empty = any")
    rate_limit_per_minute = models.PositiveIntegerField(default=120)
    expires_at = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    service_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="api_clients")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @staticmethod
    def hash_secret(secret):
        return hashlib.sha256(secret.encode()).hexdigest()

    def issue_token(self):
        """Generate a new token; only its hash is stored. Returns the token to show once."""
        self.token_id = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        self.token_hash = self.hash_secret(secret)
        return f"{self.TOKEN_PREFIX}{self.token_id}_{secret}"

    @property
    def is_usable(self):
        return self.is_active and not self.revoked_at and (not self.expires_at or self.expires_at >= timezone.localdate())


class ApiRequestLog(models.Model):
    client = models.ForeignKey(ApiClient, null=True, blank=True, on_delete=models.SET_NULL, related_name="requests")
    channel = models.CharField(max_length=8, default="api")  # api | mcp
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=300)
    operation = models.CharField(max_length=80, blank=True)
    status = models.PositiveSmallIntegerField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class OutboundMessage(models.Model):
    """Delivery log for e-mail and SMS."""

    class Channel(models.TextChoices):
        EMAIL = "email", "E-mail"
        SMS = "sms", "SMS"

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped (channel disabled)"

    channel = models.CharField(max_length=6, choices=Channel.choices)
    recipient = models.CharField(max_length=254)
    subject = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices)
    provider_reference = models.CharField(max_length=120, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
