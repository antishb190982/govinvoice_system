import hashlib
import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from core.fields import EncryptedTextField


class User(AbstractUser):
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=200)
    job_title = models.CharField(max_length=120, blank=True)
    employee_id = models.CharField(max_length=40, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    totp_secret = EncryptedTextField(blank=True)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    # Last accepted TOTP time-step, so a code cannot be replayed.
    totp_last_step = models.BigIntegerField(default=0)
    must_change_password = models.BooleanField(default=False)
    password_changed_at = models.DateTimeField(null=True, blank=True)

    REQUIRED_FIELDS = ["email", "full_name"]

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name or self.username

    def get_full_name(self):
        return self.full_name or self.username

    @property
    def has_two_factor(self):
        return bool(self.totp_secret and self.totp_confirmed_at)

    def set_password(self, raw_password):
        super().set_password(raw_password)
        self.password_changed_at = timezone.now()


class RecoveryCode(models.Model):
    """Single-use backup codes for two-factor login, stored as SHA-256 hashes."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)

    @staticmethod
    def _hash(code):
        return hashlib.sha256(code.replace("-", "").strip().lower().encode()).hexdigest()

    @classmethod
    def regenerate(cls, user, count=10):
        cls.objects.filter(user=user).delete()
        codes = []
        for _ in range(count):
            raw = secrets.token_hex(5)
            codes.append(f"{raw[:5]}-{raw[5:]}")
            cls.objects.create(user=user, code_hash=cls._hash(raw))
        return codes

    @classmethod
    def consume(cls, user, code):
        match = cls.objects.filter(user=user, code_hash=cls._hash(code), used_at__isnull=True).first()
        if not match:
            return False
        match.used_at = timezone.now()
        match.save(update_fields=["used_at"])
        return True


class LoginAttempt(models.Model):
    username = models.CharField(max_length=150, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)
    stage = models.CharField(max_length=10, default="password")  # password | otp
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
