"""
Internal certification authority for issuing staff signing certificates.

For production use, prefer certificates from the national PKI (import them as
PKCS#12) or keep this CA key in an HSM; the CA key here is encrypted with
SIGNING_CA_PASSPHRASE and stored in SIGNING_CA_DIR with 0600 permissions.
"""

import datetime
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DOCUMENT_SIGNING_EKU = ObjectIdentifier("1.3.6.1.5.5.7.3.36")


def _paths():
    return settings.SIGNING_CA_DIR / "ca.key", settings.SIGNING_CA_DIR / "ca.crt"


def _passphrase():
    if not settings.SIGNING_CA_PASSPHRASE:
        raise ImproperlyConfigured("SIGNING_CA_PASSPHRASE is not set")
    return settings.SIGNING_CA_PASSPHRASE.encode()


def ensure_ca():
    key_path, cert_path = _paths()
    if key_path.exists() and cert_path.exists():
        return
    key = ec.generate_private_key(ec.SECP384R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, settings.SIGNING_COUNTRY),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, settings.SIGNING_ORGANISATION),
        x509.NameAttribute(NameOID.COMMON_NAME, settings.SIGNING_CA_NAME),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA384())
    )
    settings.SIGNING_CA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.BestAvailableEncryption(_passphrase())))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ca_certificate():
    ensure_ca()
    return x509.load_pem_x509_certificate(_paths()[1].read_bytes())


def _ca_key():
    ensure_ca()
    return serialization.load_pem_private_key(_paths()[0].read_bytes(), password=_passphrase())


def trust_roots():
    roots = [ca_certificate()]
    for path in settings.SIGNING_EXTRA_TRUST_ROOTS:
        roots.append(x509.load_pem_x509_certificate(open(path, "rb").read()))
    return roots


def issue_certificate(user, public_key):
    ca_cert, ca_key = ca_certificate(), _ca_key()
    attrs = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, settings.SIGNING_COUNTRY),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, settings.SIGNING_ORGANISATION),
        x509.NameAttribute(NameOID.COMMON_NAME, user.get_full_name()),
    ]
    if user.job_title:
        attrs.append(x509.NameAttribute(NameOID.TITLE, user.job_title))
    if user.employee_id:
        attrs.append(x509.NameAttribute(NameOID.SERIAL_NUMBER, user.employee_id))
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder().subject_name(x509.Name(attrs)).issuer_name(ca_cert.subject)
        .public_key(public_key).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=settings.SIGNING_CERT_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([DOCUMENT_SIGNING_EKU, ExtendedKeyUsageOID.EMAIL_PROTECTION]),
                       critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
    )
    if user.email:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(user.email)]), critical=False)
    return builder.sign(ca_key, hashes.SHA256())
