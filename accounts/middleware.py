import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse

OTP_SESSION_KEY = "otp_verified"
ACTIVITY_SESSION_KEY = "last_activity"


class IdleTimeoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = int(time.time())
            last = request.session.get(ACTIVITY_SESSION_KEY, now)
            if now - last > settings.SESSION_IDLE_TIMEOUT_MINUTES * 60:
                logout(request)
                messages.info(request, "You were signed out after a period of inactivity.")
                return redirect(settings.LOGIN_URL)
            request.session[ACTIVITY_SESSION_KEY] = now
        return self.get_response(request)


class RequireTwoFactorMiddleware:
    """
    A password alone never grants access: until the session has passed the
    second factor, only the 2FA pages and sign-out are reachable. Accounts
    flagged `must_change_password` are then held on the password page.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            path = request.path
            if path.startswith(settings.STATIC_URL if settings.STATIC_URL.startswith("/") else "/" + settings.STATIC_URL):
                return self.get_response(request)
            otp_paths = {reverse("accounts:otp_verify"), reverse("accounts:otp_enroll"), reverse("accounts:logout")}
            if not request.session.get(OTP_SESSION_KEY):
                if path not in otp_paths:
                    target = "accounts:otp_verify" if user.has_two_factor else "accounts:otp_enroll"
                    return redirect(target)
            elif user.must_change_password and path not in {reverse("accounts:password_change"),
                                                            reverse("accounts:logout")}:
                return redirect("accounts:password_change")
        return self.get_response(request)
