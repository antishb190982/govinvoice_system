"""Development schema preparation for Publish's managed PostgreSQL schema diff."""

from .settings import *  # noqa: F403
from .runtime_database import postgres_config

if PRODUCTION or env("REPLIT_ENVIRONMENT") == "production":  # noqa: F405
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("This schema-preparation command is development-only.")

DATABASES = {"default": postgres_config(env("DATABASE_URL"))}  # noqa: F405
