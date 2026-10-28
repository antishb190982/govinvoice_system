"""
GovBills settings.

Every secret and deployment-specific value comes from the environment. The
defaults are safe for local development only; `GOVBILLS_ENV=production`
switches on the strict checks in `check_production_settings` below.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


ENVIRONMENT = env("GOVBILLS_ENV", "development")
PRODUCTION = ENVIRONMENT == "production"

SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me" if not PRODUCTION else None)
DEBUG = env_bool("DJANGO_DEBUG", not PRODUCTION)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.forms",
    "core",
    "accounts",
    "org",
    "providers",
    "invoices",
    "imports",
    "workflow",
    "signatures",
    "settlements",
    "analytics",
    "ocr",
    "integrations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "integrations.middleware.RuntimeConfigMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "core.middleware.PendingMigrationsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "accounts.middleware.IdleTimeoutMiddleware",
    "accounts.middleware.RequireTwoFactorMiddleware",
    "core.middleware.RequestContextMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.app_context",
            ],
        },
    },
]

FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

WSGI_APPLICATION = "config.wsgi.application"

if env("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", "govbills"),
            "USER": env("POSTGRES_USER", "govbills"),
            "PASSWORD": env("POSTGRES_PASSWORD", ""),
            "HOST": env("POSTGRES_HOST", "localhost"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"sslmode": env("POSTGRES_SSLMODE", "prefer")},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "var" / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

# --- Authentication & passwords -------------------------------------------
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "accounts.validators.ComplexityValidator"},
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

LOGIN_MAX_FAILURES = int(env("LOGIN_MAX_FAILURES", "5"))
LOGIN_LOCKOUT_MINUTES = int(env("LOGIN_LOCKOUT_MINUTES", "15"))
SESSION_IDLE_TIMEOUT_MINUTES = int(env("SESSION_IDLE_TIMEOUT_MINUTES", "15"))
SESSION_COOKIE_AGE = int(env("SESSION_MAX_HOURS", "8")) * 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_HTTPONLY = True
OTP_ISSUER = env("OTP_ISSUER", "GovBills Mauritius")

# --- Transport security (production) ----------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if PRODUCTION:
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_NAME = "__Host-sessionid"
    CSRF_COOKIE_NAME = "__Host-csrftoken"

# --- Internationalisation ----------------------------------------------------
LANGUAGE_CODE = "en-gb"
TIME_ZONE = env("TIME_ZONE", "Indian/Mauritius")
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True

# --- Static & private files --------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "var" / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" if PRODUCTION
                    else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
# A missing manifest entry must degrade to the plain URL, never turn every page into a 500.
WHITENOISE_MANIFEST_STRICT = False
# Uploaded invoices, imports and signed PDFs. Never served directly: every
# download goes through a permission-checked view. There is deliberately no
# MEDIA_URL route.
MEDIA_ROOT = Path(env("PRIVATE_STORAGE_ROOT", str(BASE_DIR / "var" / "private")))
MAX_UPLOAD_MB = int(env("MAX_UPLOAD_MB", "20"))
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_PERMISSIONS = 0o640

# --- Field encryption (bank details, signing material) ----------------------
# Comma separated Fernet keys. The first encrypts, every key decrypts, so
# keys can be rotated by prepending a new one.
FIELD_ENCRYPTION_KEYS = env_list(
    "FIELD_ENCRYPTION_KEYS",
    "" if PRODUCTION else "dKmXGzVjHGsW2yQ1vO0kAbAFxdWf2Vb0r2yK8-8fWyU=",
)

# --- Signatures --------------------------------------------------------------
SIGNING_CA_DIR = Path(env("SIGNING_CA_DIR", str(BASE_DIR / "var" / "ca")))
SIGNING_CA_PASSPHRASE = env("SIGNING_CA_PASSPHRASE", "" if PRODUCTION else "dev-ca-passphrase")
SIGNING_CA_NAME = env("SIGNING_CA_NAME", "GovBills Internal Signing CA (Mauritius)")
SIGNING_ORGANISATION = env("SIGNING_ORGANISATION", "Government of Mauritius")
SIGNING_COUNTRY = env("SIGNING_COUNTRY", "MU")
SIGNING_CERT_DAYS = int(env("SIGNING_CERT_DAYS", "730"))
SIGNING_PIN_MAX_FAILURES = int(env("SIGNING_PIN_MAX_FAILURES", "5"))
# Optional RFC 3161 timestamp authority; adds PAdES B-T timestamps when set.
SIGNING_TSA_URL = env("SIGNING_TSA_URL", "")
# Extra trusted root certificates (PEM files), e.g. the national PKI root,
# so signatures from imported PKCS#12 identities validate.
SIGNING_EXTRA_TRUST_ROOTS = env_list("SIGNING_EXTRA_TRUST_ROOTS", "")

# TTF with Arabic glyphs for local-language names on generated PDFs.
PDF_UNICODE_FONT = env("PDF_UNICODE_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

# --- OCR ----------------------------------------------------------------------
TESSERACT_CMD = env("TESSERACT_CMD", "")
OCR_LANGUAGES = env("OCR_LANGUAGES", "eng+fra")
OCR_DPI = int(env("OCR_DPI", "300"))
# Required fields below this confidence (0-1) must be confirmed by a person.
OCR_REVIEW_THRESHOLD = float(env("OCR_REVIEW_THRESHOLD", "0.8"))
# Uploads with more files than this are analysed by the background worker.
OCR_SYNC_MAX_FILES = int(env("OCR_SYNC_MAX_FILES", "3"))

# --- Business rules ----------------------------------------------------------
DEFAULT_CURRENCY = env("DEFAULT_CURRENCY", "MUR")
# Working week used for SLA clocks; Monday=0 .. Sunday=6.
WORK_WEEK_DAYS = [int(d) for d in env_list("WORK_WEEK_DAYS", "0,1,2,3,4")]
WORK_DAY_START_HOUR = int(env("WORK_DAY_START_HOUR", "9"))
WORK_DAY_END_HOUR = int(env("WORK_DAY_END_HOUR", "16"))
# Approvals must finish this many working days before an invoice falls due,
# leaving time to issue the settlement instruction and transfer the money.
SETTLEMENT_LEAD_DAYS = int(env("SETTLEMENT_LEAD_DAYS", "3"))
DUE_SOON_DAYS = int(env("DUE_SOON_DAYS", "7"))
ANOMALY_THRESHOLD_PCT = int(env("ANOMALY_THRESHOLD_PCT", "30"))
PAYER_NAME = env("PAYER_NAME", "Treasury – Accountant-General, Ministry of Finance")
PAYER_IBAN = env("PAYER_IBAN", "")
PAYER_BIC = env("PAYER_BIC", "")

# --- Email ---------------------------------------------------------------
# The configurable backend sends through the SMTP settings managed in the admin
# console, and falls back to the console backend while SMTP is disabled.
EMAIL_BACKEND = env("EMAIL_BACKEND", "integrations.mail.ConfigurableEmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "govbills@localhost")

ADMIN_URL_PATH = env("ADMIN_URL_PATH", "sysadmin/")
BRAND_NAME = env("BRAND_NAME", "GovBills")
BRAND_HIGHLIGHT = env("BRAND_HIGHLIGHT", "Bills")
BRAND_TAGLINE = env("BRAND_TAGLINE", "Government utility invoice management · Republic of Mauritius")
SITE_URL = env("SITE_URL", "http://localhost:8000")

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
if env("REDIS_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": env("REDIS_URL")}}
API_DEFAULT_RATE_PER_MINUTE = int(env("API_DEFAULT_RATE_PER_MINUTE", "120"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {"django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False}},
}


def check_production_settings():
    if not PRODUCTION:
        return
    missing = [name for name, value in {
        "DJANGO_SECRET_KEY": SECRET_KEY,
        "FIELD_ENCRYPTION_KEYS": FIELD_ENCRYPTION_KEYS,
        "SIGNING_CA_PASSPHRASE": SIGNING_CA_PASSPHRASE,
    }.items() if not value]
    if missing:
        raise ImproperlyConfigured("Missing required production settings: " + ", ".join(missing))
    if DEBUG:
        raise ImproperlyConfigured("DJANGO_DEBUG must be off in production")


check_production_settings()

for _path in (BASE_DIR / "var", STATIC_ROOT, MEDIA_ROOT, SIGNING_CA_DIR):
    _path.mkdir(parents=True, exist_ok=True)
