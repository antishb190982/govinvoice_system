from django.conf import settings
from django.db import models
from django.utils import timezone


class SigningIdentity(models.Model):
    """
    A user's X.509 signing certificate and private key. The key is encrypted
    twice: PKCS#8 under the user's signing PIN, then with the server field
    key — so a database copy alone, or the server key alone, cannot sign.
    """

    class Source(models.TextChoices):
        INTERNAL = "internal", "Issued by internal CA"
        IMPORTED = "imported", "Imported PKCS#12 (external CA)"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="signing_identities")
    source = models.CharField(max_length=10, choices=Source.choices)
    certificate_pem = models.TextField()
    encrypted_key = models.BinaryField()
    serial_number = models.CharField(max_length=64)
    fingerprint_sha256 = models.CharField(max_length=64, unique=True)
    subject = models.CharField(max_length=500)
    issuer = models.CharField(max_length=500)
    not_before = models.DateTimeField()
    not_after = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.CharField(max_length=200, blank=True)
    failed_pin_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subject} ({self.serial_number[:12]})"

    @property
    def is_usable(self):
        now = timezone.now()
        return self.is_active and not self.revoked_at and self.not_before <= now <= self.not_after

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())


class Signature(models.Model):
    """A detached signature over a canonical JSON payload (e.g. an approval decision)."""

    identity = models.ForeignKey(SigningIdentity, on_delete=models.PROTECT, related_name="signatures")
    purpose = models.CharField(max_length=80, db_index=True)
    payload = models.JSONField()
    payload_sha256 = models.CharField(max_length=64)
    algorithm = models.CharField(max_length=40)
    signature = models.BinaryField()
    signed_at = models.DateTimeField()
    approval_task = models.OneToOneField("workflow.ApprovalTask", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="signature")

    class Meta:
        ordering = ["-signed_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            # Only the link to the task may be set after creation.
            kwargs["update_fields"] = ["approval_task"]
        super().save(*args, **kwargs)
