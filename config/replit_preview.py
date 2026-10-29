"""Development-only adapter for Replit's HTTPS proxy and embedded preview."""

from .settings import *  # noqa: F403
from .temporary_auth import FIXED_OTP_CODE

REPLIT_PREVIEW = True
if PRODUCTION:  # noqa: F405
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("Use config.settings, not the demo preview settings, in production.")

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver", ".replit.dev"]
CSRF_TRUSTED_ORIGINS = ["https://*.replit.dev"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Preview is an HTTPS iframe on a different site from the workspace editor.
SESSION_COOKIE_SAMESITE = "None"
CSRF_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "SAMEORIGIN"

MIDDLEWARE = [
    "config.preview_support.PreviewHeadersMiddleware"
    if item == "core.middleware.SecurityHeadersMiddleware" else item
    for item in MIDDLEWARE  # noqa: F405
    if item != "django.middleware.clickjacking.XFrameOptionsMiddleware"
]
TEMPLATES[0]["OPTIONS"]["context_processors"].append(  # noqa: F405
    "config.preview_support.preview_context"
)

# Keep demo notifications local. Publishing uses the original settings instead.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
