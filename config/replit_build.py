"""Build hashed static assets without seeding data or relaxing runtime security."""

from .settings import *  # noqa: F403

STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
