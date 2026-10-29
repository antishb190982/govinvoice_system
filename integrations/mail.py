"""E-mail backend driven by the SMTP settings in the admin console, with a delivery log."""

from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.core.mail.backends.smtp import EmailBackend as SmtpBackend

from integrations import config


def _log(message, status, error=""):
    from integrations.models import OutboundMessage

    for to in message.to:
        OutboundMessage.objects.create(channel=OutboundMessage.Channel.EMAIL, recipient=to[:254],
                                       notification_id=getattr(message, "govbills_notification_id", None),
                                       subject=message.subject[:200], status=status, error=error[:2000])


class ConfigurableEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        from integrations.models import OutboundMessage

        config.refresh()
        if not config.get("SMTP_ENABLED") or not config.get("SMTP_HOST"):
            # Development / not yet configured: print, and log as skipped.
            sent = ConsoleBackend(fail_silently=True).send_messages(email_messages)
            for m in email_messages:
                _log(m, OutboundMessage.Status.SKIPPED, "SMTP disabled")
            return sent
        security = config.get("SMTP_SECURITY")
        backend = SmtpBackend(
            host=config.get("SMTP_HOST"), port=int(config.get("SMTP_PORT") or 587),
            username=config.get("SMTP_USERNAME") or None, password=config.get("SMTP_PASSWORD") or None,
            use_tls=security == "starttls", use_ssl=security == "ssl", timeout=int(config.get("SMTP_TIMEOUT") or 15),
            fail_silently=self.fail_silently,
        )
        sender = config.get("SMTP_FROM")
        for m in email_messages:
            if sender and (not m.from_email or m.from_email.endswith("@localhost")):
                m.from_email = sender
        sent = 0
        for m in email_messages:
            try:
                count = backend.send_messages([m]) or 0
                sent += count
                _log(m, OutboundMessage.Status.SENT if count else OutboundMessage.Status.FAILED)
            except Exception as exc:
                _log(m, OutboundMessage.Status.FAILED, f"{exc.__class__.__name__}: {exc}")
                if not self.fail_silently:
                    raise
        return sent
