"""Trusted-relay boundary: never trust authenticity claims inside an e-mail."""
import hashlib
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import AuditEvent
from imports.bulk import SUPPORTED, stage_uploads
from integrations.gateway import ApiError, endpoint
from integrations.inbound_models import EbillDelivery, EbillMailbox, EbillSender
from org.access import active_memberships
from org.models import Role

MAX_MESSAGE_BYTES = 25 * 1024 * 1024


def relay_problem(client):
    """A scoped relay cannot use a more powerful capture identity."""
    if not client.is_usable or set(client.scopes) != {"mail:ingest"}:
        return "Select an active, unexpired, single-purpose e-bill relay client."
    user = client.service_user
    if not user.is_active or user.is_superuser:
        return "The relay needs an active, non-superuser processing account."
    memberships = list(active_memberships(user, [Role.CLERK, Role.UNIT_ADMIN]).select_related("org_unit"))
    if not memberships:
        return "The processing account needs an active clerk or unit-administrator role."
    if client.org_unit_id and any(not m.org_unit.path.startswith(client.org_unit.path) for m in memberships):
        return "The processing account's capture permissions exceed the relay's unit scope."
    return ""


def parse_message(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    headers = message.get_all("From", [])
    if len(headers) != 1 or len(headers[0].addresses) != 1:
        raise ValidationError("Exactly one valid From address is required.")
    sender = headers[0].addresses[0].addr_spec.strip().lower()
    validate_email(sender)
    return message, sender


def _state(delivery, state, reason, actor=None):
    previous = delivery.state
    delivery.state, delivery.reason = state, reason[:500]
    delivery.save(update_fields=["state", "reason", "provider"])
    AuditEvent.record("mail.intake_state", obj=delivery, actor=actor, data={
        "from": previous, "to": state, "reason": reason[:500], "sha256": delivery.sha256})
    return delivery


def _attachments(message):
    files, pending = [], [(message, 0)]
    total_parts = total_bytes = 0
    while pending:
        part, depth = pending.pop()
        total_parts += 1
        if depth > 20 or total_parts > 250:
            raise ValidationError("The message has too many or deeply nested MIME parts.")
        if part.is_multipart():
            pending.extend((child, depth + 1) for child in part.iter_parts())
            continue
        name = part.get_filename()
        if not name:
            continue
        # Do not turn inline logos into invoice records.
        if part.get_content_disposition() == "inline" and part.get_content_type() != "application/pdf":
            continue
        path = PurePosixPath(name)
        if path.suffix.lower() not in SUPPORTED | {".zip"}:
            raise ValidationError("An attachment has an unsupported file type.")
        if path.name != name or "\\" in name or ":" in name or name in {".", ".."}:
            raise ValidationError("An attachment has an unsafe file name.")
        content = part.get_payload(decode=True)
        if not content:
            raise ValidationError("An attachment is empty or could not be decoded.")
        total_bytes += len(content)
        if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024 or total_bytes > MAX_MESSAGE_BYTES or len(files) >= 100:
            raise ValidationError("The message exceeds the invoice attachment limits.")
        files.append(SimpleUploadedFile(name, content, part.get_content_type()))
    if not files:
        raise ValidationError("No supported invoice attachments were found.")
    return files


@transaction.atomic
def process_delivery(delivery_id, *, manual=False, actor=None):
    delivery = EbillDelivery.objects.select_for_update(of=("self",)).select_related(
        "mailbox", "relay_client__service_user", "relay_client__org_unit").get(pk=delivery_id)
    if delivery.state == EbillDelivery.State.STAGED:
        return delivery
    if not delivery.mailbox.enabled:
        return _state(delivery, "rejected", "Mailbox intake is disabled.", actor)
    if delivery.mailbox.relay_client_id != delivery.relay_client_id:
        return _state(delivery, "rejected", "This relay is no longer authorised for this mailbox.", actor)
    problem = relay_problem(delivery.relay_client)
    if problem:
        return _state(delivery, "rejected", problem, actor)
    if not delivery.authentication_passed or delivery.sender != delivery.authenticated_sender:
        return _state(delivery, "rejected", "Trusted sender authentication did not pass.", actor)
    rule = EbillSender.objects.filter(mailbox=delivery.mailbox, address=delivery.sender,
                                      enabled=True, provider__is_active=True).select_related("provider").first()
    if not rule:
        return _state(delivery, "rejected", "No enabled sender-to-provider rule matches this address.", actor)
    delivery.provider = rule.provider
    if not delivery.attachments_clean:
        return _state(delivery, "quarantined", "The trusted relay has not attested clean attachment-scan results.", actor)
    if not delivery.mailbox.auto_stage and not manual:
        return _state(delivery, "held", "Automatic staging is off. An administrator may stage this delivery.", actor)
    try:
        with transaction.atomic():
            with delivery.raw_message.open("rb") as source:
                raw = source.read(MAX_MESSAGE_BYTES + 1)
            if len(raw) > MAX_MESSAGE_BYTES or hashlib.sha256(raw).hexdigest() != delivery.sha256:
                raise ValidationError("Stored message integrity check failed.")
            message, sender = parse_message(raw)
            if sender != delivery.sender:
                raise ValidationError("Stored sender evidence does not match.")
            batches, skipped = stage_uploads(
                delivery.relay_client.service_user, _attachments(message), rule.provider,
                received_date=timezone.localtime(delivery.received_at).date(), intake_source="email")
            if skipped:
                raise ValidationError("An attachment or ZIP entry was skipped; review the original message.")
            delivery.import_batches.set(batches)
            AuditEvent.record("mail.invoices_staged", obj=delivery, actor=actor,
                              data={"batch_ids": [batch.pk for batch in batches], "provider_id": rule.provider_id})
    except (ValidationError, PermissionDenied) as exc:
        reason = "; ".join(exc.messages) if isinstance(exc, ValidationError) else "The processing account lacks capture permission."
        return _state(delivery, "failed", reason, actor)
    except Exception as exc:
        # Do not put storage credentials or message contents in an error response/log.
        return _state(delivery, "failed", "Processing failed (" + type(exc).__name__ + "). Retry after checking the server.", actor)
    return _state(delivery, "staged", "Attachments staged for invoice checks; no invoices were approved or paid.", actor)


@endpoint(scope="mail:ingest", methods=("POST",), operation="mail.receive")
def receive(request, client):
    recipient = request.POST.get("mailbox", "").strip().lower()
    mailbox = EbillMailbox.objects.filter(address=recipient).first()
    if not mailbox or mailbox.relay_client_id != client.pk:
        raise ApiError(403, "mailbox_not_authorised", "This relay is not authorised for this mailbox.")
    if not mailbox.enabled or relay_problem(client):
        raise ApiError(403, "mailbox_disabled", "Mailbox intake or the processing identity is not enabled.")
    delivery_id = request.POST.get("delivery_id", "").strip()
    try:
        received_at = parse_datetime(request.POST.get("received_at", ""))
    except (ValueError, TypeError):
        received_at = None
    if not delivery_id or len(delivery_id) > 300 or not received_at or timezone.is_naive(received_at):
        raise ApiError(400, "invalid_metadata", "Supply a delivery ID and timezone-aware trusted received_at timestamp.")
    if received_at > timezone.now() + timedelta(minutes=5):
        raise ApiError(400, "invalid_metadata", "The trusted receipt timestamp is in the future.")
    if set(request.FILES) != {"message"} or len(request.FILES.getlist("message")) != 1:
        raise ApiError(400, "message_required", "Upload exactly one complete RFC822 message.")
    upload = request.FILES.get("message")
    if not upload or upload.size > MAX_MESSAGE_BYTES:
        raise ApiError(413, "message_required", "Upload one RFC822 message of at most 25 MB.")
    raw = upload.read(MAX_MESSAGE_BYTES + 1)
    if not raw or len(raw) > MAX_MESSAGE_BYTES:
        raise ApiError(413, "message_too_large", "The message is empty or exceeds 25 MB.")
    digest = hashlib.sha256(raw).hexdigest()
    if request.POST.get("message_sha256", "").lower() != digest:
        raise ApiError(400, "invalid_evidence", "The relay's message digest does not match the uploaded message.")
    key = hashlib.sha256(delivery_id.encode()).hexdigest()
    try:
        message, sender = parse_message(raw)
        subject, message_id = str(message.get("Subject", ""))[:200], str(message.get("Message-ID", ""))[:300]
    except (ValidationError, ValueError, AttributeError, RecursionError):
        sender, subject, message_id = "", "", ""
    authenticated_sender = request.POST.get("authenticated_sender", "").strip().lower()
    try:
        validate_email(authenticated_sender)
    except ValidationError:
        authenticated_sender = ""
    with transaction.atomic():
        # Serialise mailbox deliveries, including duplicate message reservations.
        EbillMailbox.objects.select_for_update().get(pk=mailbox.pk)
        delivery = EbillDelivery.objects.filter(mailbox=mailbox, delivery_key=key).first()
        if delivery:
            if delivery.sha256 != digest:
                raise ApiError(409, "delivery_conflict", "This delivery ID was already used for different message contents.")
            return {"id": delivery.pk, "state": delivery.state, "duplicate": True}
        delivery = EbillDelivery(
            mailbox=mailbox, relay_client=client, delivery_key=key, sender=sender, subject=subject,
            message_id=message_id, received_at=received_at, sha256=digest, authenticated_sender=authenticated_sender,
            authentication_passed=bool(sender and authenticated_sender and request.POST.get("authentication_passed") == "true"),
            attachments_clean=request.POST.get("attachments_clean") == "true")
        delivery.raw_message.save("message.eml", ContentFile(raw), save=False)
        delivery.save()
        AuditEvent.record("mail.received", obj=delivery, data={"sha256": digest, "mailbox_id": mailbox.pk})
    delivery = process_delivery(delivery.pk)
    return {"id": delivery.pk, "state": delivery.state, "reason": delivery.reason, "duplicate": False}
