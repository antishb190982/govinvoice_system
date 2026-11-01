"""Development schema preparation for Publish's managed PostgreSQL schema diff."""

from .settings import *  # noqa: F403
from .runtime_database import postgres_config, require_development_schema_context

require_development_schema_context(
    ENVIRONMENT, env("REPLIT_DEPLOYMENT", ""),  # noqa: F405
)

DATABASES = {"default": postgres_config(env("DATABASE_URL"))}  # noqa: F405
