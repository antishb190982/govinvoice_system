import base64
import io
import time
from datetime import timedelta

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.utils import timezone

from accounts.models import LoginAttempt


def is_locked_out(username, ip=None):
    since = timezone.now() - timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
    recent = LoginAttempt.objects.filter(username__iexact=username, created_at__gte=since)
    last_success = recent.filter(succeeded=True, stage="otp").order_by("-created_at").first()
    failures = recent.filter(succeeded=False)
    if last_success:
        failures = failures.filter(created_at__gt=last_success.created_at)
    if failures.count() >= settings.LOGIN_MAX_FAILURES:
        return True
    # Also throttle one address spraying many usernames.
    if ip and LoginAttempt.objects.filter(ip_address=ip, succeeded=False, created_at__gte=since).count() \
            >= settings.LOGIN_MAX_FAILURES * 4:
        return True
    return False


def record_attempt(username, ip, succeeded, stage="password"):
    LoginAttempt.objects.create(username=username[:150], ip_address=ip, succeeded=succeeded, stage=stage)


def new_totp_secret():
    return pyotp.random_base32()


def provisioning_qr_data_uri(user, secret):
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email or user.username, issuer_name=settings.OTP_ISSUER)
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def verify_totp(user, code, secret=None):
    """Verify a TOTP code, rejecting replays of an already used time-step."""
    secret = secret or user.totp_secret
    code = (code or "").replace(" ", "")
    if not secret or not code.isdigit() or len(code) != 6:
        return False
    totp = pyotp.TOTP(secret)
    now = time.time()
    for drift in (0, -1, 1):
        at = now + drift * totp.interval
        if totp.verify(code, for_time=at, valid_window=0):
            step = int(at // totp.interval)
            if step <= user.totp_last_step:
                return False
            user.totp_last_step = step
            user.save(update_fields=["totp_last_step"])
            return True
    return False
