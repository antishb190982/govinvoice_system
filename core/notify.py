"""Transactional in-app alerts with independent, honest channel results."""
import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction

from core.models import Notification

log = logging.getLogger(__name__)


def _result(note_id, channel, recipient, title, status, error=""):
    from integrations.models import OutboundMessage
    OutboundMessage.objects.create(notification_id=note_id, channel=channel,
        recipient=recipient[:254], subject=title[:200], status=status, error=error[:2000])


def channel_readiness():
    from integrations import config
    backend = settings.EMAIL_BACKEND
    local = any(name in backend for name in (".locmem.", ".console.", ".filebased.", ".dummy."))
    if local:
        email = "Preview only — external email is not delivered"
    elif backend == "integrations.mail.ConfigurableEmailBackend":
        email = ("SMTP configured — see each delivery result" if
                 config.get("SMTP_ENABLED") and config.get("SMTP_HOST")
                 else "Not connected — configure the SMTP gateway")
    else:
        email = "Email transport selected — see each delivery result"
    sms = ("SMS configured — see each delivery result" if
           config.get("SMS_ENABLED") and config.get("SMS_URL")
           else "Not connected — configure the SMS gateway")
    return {"email": email, "sms": sms}


def notify(users, title, body="", link="", level=Notification.Level.INFO,
           dedupe_key="", email=True, action_required=None):
    """Persist once per event/user; external failures never erase the alert."""
    from integrations import sms
    sent, seen = 0, set()
    actionable = bool(link) if action_required is None else action_required
    for user in users:
        if not user.is_active or user.pk in seen:
            continue
        seen.add(user.pk)
        try:
            with transaction.atomic():
                note = Notification.objects.create(user=user, title=title, body=body, link=link,
                    level=level, dedupe_key=dedupe_key)
        except IntegrityError:
            continue
        sent += 1
        url = settings.SITE_URL.rstrip("/") + link if link else ""
        if email:
            if user.email:
                transaction.on_commit(lambda u=user, t=title, b=f"{body}\n\n{url}".strip(),
                    pk=note.pk: _send(u, t, b, pk), robust=True)
            else:
                _result(note.pk, "email", "Not provided", title, "skipped", "No email address")
        # All actionable alerts request SMS, not just critical severity alerts.
        # The transport records disabled/missing-number results without sending.
        if actionable or sms.wanted(level):
            if user.phone:
                text = f"{settings.BRAND_NAME}: {title}. {url}".strip()
                transaction.on_commit(lambda n=user.phone, x=text, t=title, pk=note.pk:
                    _send_sms(n, x, t, pk), robust=True)
            else:
                _result(note.pk, "sms", "Not provided", title, "skipped", "No mobile number")
    return sent


def _send_sms(number, text, title, note_id=None):
    from integrations import sms
    try:
        sms.send(number, text, subject=title, notification_id=note_id)
    except Exception:
        _result(note_id, "sms", sms.mask(number), title, "failed", "SMS transport failed")
        log.exception("SMS delivery failed")


def _send(user, title, body, note_id=None):
    from integrations.models import OutboundMessage
    message = EmailMessage(f"[GovBills] {title}", body, None, [user.email])
    message.govbills_notification_id = note_id
    try:
        count = message.send(fail_silently=False)
        # The configurable SMTP backend already records its actual result.
        if settings.EMAIL_BACKEND != "integrations.mail.ConfigurableEmailBackend":
            local = any(name in settings.EMAIL_BACKEND for name in
                        (".locmem.", ".console.", ".filebased.", ".dummy."))
            _result(note_id, "email", user.email, title,
                    "skipped" if local else ("sent" if count else "failed"),
                    "Preview backend — no external email delivery" if local else "")
    except Exception:
        if not OutboundMessage.objects.filter(notification_id=note_id, channel="email").exists():
            _result(note_id, "email", user.email, title, "failed", "Email transport failed")
        log.exception("Could not e-mail notification to %s", user.pk)
