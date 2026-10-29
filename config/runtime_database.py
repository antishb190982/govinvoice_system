"""Parse the runtime PostgreSQL URL without logging credentials."""

from urllib.parse import parse_qs, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured


def postgres_config(url):
    try:
        parsed = urlsplit(url or "")
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {"sslmode": parse_qs(parsed.query).get("sslmode", ["require"])[0]},
        }
    except (ValueError, TypeError):
        raise ImproperlyConfigured("A valid PostgreSQL DATABASE_URL is required.") from None
