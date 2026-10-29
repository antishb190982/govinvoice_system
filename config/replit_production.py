"""Production adapter: managed PostgreSQL, private App Storage, original security."""

from .production_secrets import configure_runtime_secrets

configure_runtime_secrets()

from .settings import *  # noqa: E402,F403
from .runtime_database import postgres_config  # noqa: E402
from .temporary_auth import FIXED_OTP_CODE  # noqa: E402

if not PRODUCTION:  # noqa: F405
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("The production service requires GOVBILLS_ENV=production.")
if not env("PRIVATE_OBJECT_DIR") or not env("DEFAULT_OBJECT_STORAGE_BUCKET_ID"):  # noqa: F405
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("Private App Storage is required for the production portal.")

DATABASES = {"default": postgres_config(env("DATABASE_URL"))}  # noqa: F405
STORAGES["default"] = {"BACKEND": "core.app_storage.PrivateAppStorage"}  # noqa: F405
SIGNING_CA_OBJECT_STORAGE = True
GOVBILLS_SETUP_TOKEN = env("GOVBILLS_SETUP_TOKEN", "")  # noqa: F405
ROOT_URLCONF = "config.replit_urls"
ALLOWED_HOSTS = list(dict.fromkeys(ALLOWED_HOSTS + ["localhost", "127.0.0.1"]))  # noqa: F405
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]

# Publish applies schema, not necessarily Django's migration-history rows.
# Readiness checks the actual tables instead; runtime never applies DDL.
MIDDLEWARE = [
    "core.production_readiness.ProductionReadinessMiddleware"
    if item == "core.middleware.PendingMigrationsMiddleware" else item
    for item in MIDDLEWARE  # noqa: F405
]
