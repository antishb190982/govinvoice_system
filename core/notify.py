import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction

from core.models import Notification

log = logging.getLogger(__name__)


def notify(users, title, body="", link="", level=Notification.Level.INFO, dedupe_key="", email=True):
    """In-app notification plus e-mail. With a dedupe_key a user is told only once."""
    sent = 0
    for user in users:
        try:
            with transaction.atomic():
                Notification.objects.create(user=user, title=title, body=body, link=link, level=level,
                                            dedupe_key=dedupe_key)
        except IntegrityError:
            continue
        sent += 1
        if email and user.email:
            url = settings.SITE_URL.rstrip("/") + link if link else ""
            transaction.on_commit(lambda u=user, t=title, b=f"{body}\n\n{url}".strip(): _send(u, t, b))
    return sent


def _send(user, title, body):
    try:
        send_mail(f"[GovBills] {title}", body, None, [user.email], fail_silently=False)
    except Exception:  # mail outages must not break the workflow
        log.exception("Could not e-mail notification to %s", user.pk)
