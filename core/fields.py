from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _fernet():
    keys = settings.FIELD_ENCRYPTION_KEYS
    if not keys:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEYS is not set")
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    return _fernet().decrypt(token)


class EncryptedTextField(models.TextField):
    """Text encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256)."""

    PREFIX = "enc:"

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value
        return self.PREFIX + _fernet().encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value in (None, "") or not value.startswith(self.PREFIX):
            return value
        try:
            return _fernet().decrypt(value[len(self.PREFIX):].encode()).decode()
        except InvalidToken:
            return "<undecryptable>"
