"""Purpose-separated production keys; never print or persist plaintext secrets."""

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.core.exceptions import ImproperlyConfigured


def configure_runtime_secrets():
    django_key = os.environ.get("DJANGO_SECRET_KEY") or os.environ.get("SESSION_SECRET", "")
    if len(django_key) < 32:
        raise ImproperlyConfigured("Set a strong DJANGO_SECRET_KEY or SESSION_SECRET (at least 32 characters).")
    os.environ["DJANGO_SECRET_KEY"] = django_key
    missing_data_keys = [
        name for name in ("FIELD_ENCRYPTION_KEYS", "SIGNING_CA_PASSPHRASE")
        if not os.environ.get(name)
    ]
    if missing_data_keys:
        master = os.environ.get("GOVBILLS_DATA_SECRET", "")
        if len(master) < 32:
            raise ImproperlyConfigured(
                "Set GOVBILLS_DATA_SECRET to a stable random value of at least 32 characters. "
                "It protects permanent data and signing material; do not rotate it casually."
            )
        for name, purpose in (
            ("FIELD_ENCRYPTION_KEYS", b"govbills/field-encryption/v1"),
            ("SIGNING_CA_PASSPHRASE", b"govbills/signing-ca/v1"),
        ):
            if name in missing_data_keys:
                raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(master.encode())
                os.environ[name] = base64.urlsafe_b64encode(raw).decode()
