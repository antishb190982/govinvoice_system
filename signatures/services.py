import base64
import hashlib
import io
from datetime import timedelta

from asn1crypto import keys as asn1_keys
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.fields import decrypt_bytes, encrypt_bytes
from core.models import AuditEvent, canonical_json
from signatures import ca
from signatures.models import Signature, SigningIdentity

MIN_PIN_LENGTH = 8
PIN_LOCK_MINUTES = 30


def validate_pin(pin):
    if not pin or len(pin) < MIN_PIN_LENGTH:
        raise ValidationError(f"The signing PIN must have at least {MIN_PIN_LENGTH} characters.")
    if len(set(pin)) < 4:
        raise ValidationError("The signing PIN is too repetitive.")


def _store_identity(user, private_key, certificate, pin, source):
    validate_pin(pin)
    pkcs8 = private_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                      serialization.BestAvailableEncryption(pin.encode()))
    with transaction.atomic():
        SigningIdentity.objects.filter(user=user, is_active=True).update(is_active=False)
        identity = SigningIdentity.objects.create(
            user=user, source=source,
            certificate_pem=certificate.public_bytes(serialization.Encoding.PEM).decode(),
            encrypted_key=encrypt_bytes(pkcs8),
            serial_number=format(certificate.serial_number, "x"),
            fingerprint_sha256=certificate.fingerprint(hashes.SHA256()).hex(),
            subject=certificate.subject.rfc4514_string(),
            issuer=certificate.issuer.rfc4514_string(),
            not_before=certificate.not_valid_before_utc,
            not_after=certificate.not_valid_after_utc,
        )
        AuditEvent.record("signature.identity_created", obj=identity, actor=user,
                          data={"source": source, "serial": identity.serial_number,
                                "fingerprint": identity.fingerprint_sha256})
    return identity


def enroll(user, pin):
    """Generate a P-256 key pair and have the internal CA certify it."""
    key = ec.generate_private_key(ec.SECP256R1())
    cert = ca.issue_certificate(user, key.public_key())
    return _store_identity(user, key, cert, pin, SigningIdentity.Source.INTERNAL)


def import_pkcs12(user, data, password, pin):
    try:
        key, cert, _ = pkcs12.load_key_and_certificates(data, password.encode() if password else None)
    except ValueError:
        raise ValidationError("The PKCS#12 file could not be opened; check the password.")
    if key is None or cert is None:
        raise ValidationError("The PKCS#12 file must contain a private key and its certificate.")
    if not isinstance(key, (ec.EllipticCurvePrivateKey, rsa.RSAPrivateKey)):
        raise ValidationError("Only RSA and EC keys are supported.")
    now = timezone.now()
    if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
        raise ValidationError("The certificate is not currently valid.")
    if _find_issuer(cert) is None:
        raise ValidationError("The certificate is not issued by a trusted CA. Ask the administrator to add the "
                              "issuing CA to SIGNING_EXTRA_TRUST_ROOTS.")
    return _store_identity(user, key, cert, pin, SigningIdentity.Source.IMPORTED)


def active_identity(user):
    return SigningIdentity.objects.filter(user=user, is_active=True, revoked_at__isnull=True).first()


def revoke(identity, reason, actor):
    identity.revoked_at = timezone.now()
    identity.revocation_reason = reason
    identity.is_active = False
    identity.save(update_fields=["revoked_at", "revocation_reason", "is_active"])
    AuditEvent.record("signature.identity_revoked", obj=identity, actor=actor, data={"reason": reason})


def unlock_key(identity, pin):
    """Decrypt the private key, counting failures and locking after too many."""
    if not identity.is_usable:
        raise PermissionDenied("Your signing certificate is not valid. Renew it on the Signatures page.")
    if identity.is_locked:
        raise PermissionDenied("Signing is locked after repeated wrong PINs. Try again later.")
    try:
        key = serialization.load_der_private_key(decrypt_bytes(bytes(identity.encrypted_key)),
                                                 password=(pin or "").encode())
    except (ValueError, TypeError):
        identity.failed_pin_attempts += 1
        fields = ["failed_pin_attempts"]
        if identity.failed_pin_attempts >= settings.SIGNING_PIN_MAX_FAILURES:
            identity.locked_until = timezone.now() + timedelta(minutes=PIN_LOCK_MINUTES)
            identity.failed_pin_attempts = 0
            fields.append("locked_until")
        identity.save(update_fields=fields)
        AuditEvent.record("signature.pin_failed", obj=identity, actor=identity.user)
        raise ValidationError("Wrong signing PIN.")
    if identity.failed_pin_attempts:
        identity.failed_pin_attempts = 0
        identity.save(update_fields=["failed_pin_attempts"])
    return key


def check_pin(user, pin):
    """Verify the signing PIN before any surrounding transaction starts, so a wrong
    PIN's failure count is committed even though the business action is refused."""
    identity = active_identity(user)
    if identity is None:
        raise ValidationError("Set up your digital signature first.")
    unlock_key(identity, pin)


def _sign_bytes(key, data):
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return key.sign(data, ec.ECDSA(hashes.SHA256())), "ecdsa-sha256"
    return key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                    hashes.SHA256()), "rsa-pss-sha256"


def sign_payload(user, pin, payload, purpose):
    identity = active_identity(user)
    if identity is None:
        raise ValidationError("This step needs a digital signature. Set up your signing certificate first.")
    key = unlock_key(identity, pin)
    appearance = identity.signature_image.name
    appearance_hash = identity.signature_image_sha256
    if appearance:
        with identity.signature_image.open("rb") as source:
            image = source.read(2 * 1024 * 1024)
        if not appearance_hash or hashlib.sha256(image).hexdigest() != appearance_hash:
            raise ValidationError("Your signature image has changed. Upload it again before signing.")
        # Add metadata only for new image-bearing signatures. Legacy signed
        # payloads and existing invoice fingerprints remain unchanged.
        payload = {**payload, "signature_appearance": {"sha256": appearance_hash}}
    data = canonical_json(payload).encode()
    sig, algorithm = _sign_bytes(key, data)
    return Signature.objects.create(
        identity=identity, purpose=purpose, payload=payload, payload_sha256=hashlib.sha256(data).hexdigest(),
        algorithm=algorithm, signature=sig, signed_at=timezone.now(),
        appearance_image=appearance, appearance_sha256=appearance_hash if appearance else "",
    )


def _find_issuer(cert):
    for root in ca.trust_roots():
        if root.subject == cert.issuer:
            try:
                cert.verify_directly_issued_by(root)
                return root
            except (ValueError, TypeError, InvalidSignature):
                continue
    return None


def verify_signature(signature):
    """Independent check of a stored signature. Returns (valid, list_of_findings)."""
    findings = []
    identity = signature.identity
    cert = x509.load_pem_x509_certificate(identity.certificate_pem.encode())
    data = canonical_json(signature.payload).encode()
    if hashlib.sha256(data).hexdigest() != signature.payload_sha256:
        findings.append("Signed content was altered after signing.")
    public_key = cert.public_key()
    try:
        if signature.algorithm == "ecdsa-sha256":
            public_key.verify(bytes(signature.signature), data, ec.ECDSA(hashes.SHA256()))
        else:
            public_key.verify(bytes(signature.signature), data,
                              padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    except InvalidSignature:
        findings.append("Cryptographic signature does not match the content.")
    if not (cert.not_valid_before_utc <= signature.signed_at <= cert.not_valid_after_utc):
        findings.append("Certificate was not valid at signing time.")
    if identity.revoked_at and identity.revoked_at <= signature.signed_at:
        findings.append("Certificate had been revoked before signing.")
    if _find_issuer(cert) is None:
        findings.append("Certificate does not chain to a trusted CA.")
    task = signature.approval_task
    if task is not None:
        snapshot = task.invoice.snapshot()
        if snapshot != signature.payload.get("invoice"):
            findings.append("The invoice has changed since this approval was signed.")
    return not findings, findings


def signature_summary(signature):
    ok, findings = verify_signature(signature)
    return {"valid": ok, "findings": findings, "signer": signature.identity.subject,
            "serial": signature.identity.serial_number, "signed_at": signature.signed_at,
            "algorithm": signature.algorithm, "digest": signature.payload_sha256,
            "value": base64.b64encode(bytes(signature.signature)).decode()}


# --- PAdES PDF signatures -------------------------------------------------------

def sign_pdf(user, pin, pdf_bytes, field_name, reason, location="", box=None, page=0):
    """Add an incremental PAdES signature (B-B, or B-T with a TSA) to a PDF."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers, timestamps
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    identity = active_identity(user)
    if identity is None:
        raise ValidationError("Set up your signing certificate first.")
    key = unlock_key(identity, pin)
    cert = x509.load_pem_x509_certificate(identity.certificate_pem.encode())
    issuer = _find_issuer(cert)
    registry = SimpleCertificateStore.from_certs(
        [asn1_x509.Certificate.load(issuer.public_bytes(serialization.Encoding.DER))] if issuer else [])
    signer = signers.SimpleSigner(
        signing_cert=asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER)),
        signing_key=asn1_keys.PrivateKeyInfo.load(key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
        cert_registry=registry,
        prefer_pss=isinstance(key, rsa.RSAPrivateKey),
    )
    writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes), strict=False)
    meta = signers.PdfSignatureMetadata(field_name=field_name, reason=reason, location=location or None,
                                        md_algorithm="sha256", subfilter=fields.SigSeedSubFilter.PADES,
                                        name=user.get_full_name())
    timestamper = timestamps.HTTPTimeStamper(settings.SIGNING_TSA_URL) if settings.SIGNING_TSA_URL else None
    spec = fields.SigFieldSpec(sig_field_name=field_name, on_page=page, box=box) if box else None
    out = signers.sign_pdf(writer, meta, signer=signer, timestamper=timestamper, new_field_spec=spec,
                           existing_fields_only=False)
    AuditEvent.record("signature.pdf_signed", obj=identity, actor=user,
                      data={"field": field_name, "reason": reason, "sha256": hashlib.sha256(out.getvalue()).hexdigest()})
    return out.getvalue()


def validate_pdf(pdf_bytes):
    """Validate every embedded signature against the configured trust roots."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    roots = [asn1_x509.Certificate.load(c.public_bytes(serialization.Encoding.DER)) for c in ca.trust_roots()]
    reports = []
    try:
        reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
        embedded = list(reader.embedded_signatures)
    except Exception as exc:  # malformed upload
        raise ValidationError(f"The file could not be read as a PDF ({exc.__class__.__name__}).")
    for sig in embedded:
        context = ValidationContext(trust_roots=roots, allow_fetching=False, revocation_mode="soft-fail")
        try:
            status = validate_pdf_signature(sig, signer_validation_context=context)
            cert = status.signing_cert
            reports.append({
                "field": sig.field_name,
                "signer": cert.subject.human_friendly if cert else "unknown",
                "signed_at": status.signer_reported_dt,
                "timestamped": status.timestamp_validity is not None,
                "intact": status.intact,
                "valid": status.valid,
                "trusted": status.trusted,
                "coverage": status.coverage.name if status.coverage else "",
                "modifications": status.modification_level.name if status.modification_level else "",
                "ok": bool(status.bottom_line),
            })
        except Exception as exc:
            reports.append({"field": sig.field_name, "signer": "unknown", "ok": False,
                            "error": f"{exc.__class__.__name__}: {exc}"})
    return reports
