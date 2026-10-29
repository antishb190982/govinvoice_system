"""Private Django Storage backed by Replit App Storage.

No public URL is exposed. Existing permission-checked download views remain
the only route to invoice documents, staged imports and signed settlements.
"""

import io
import os
from functools import cached_property
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, SuspiciousFileOperation
from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivateAppStorage(Storage):
    @cached_property
    def client(self):
        from replit.object_storage import Client

        return Client(bucket_id=os.environ["DEFAULT_OBJECT_STORAGE_BUCKET_ID"])

    @cached_property
    def prefix(self):
        bucket, separator, directory = os.environ.get("PRIVATE_OBJECT_DIR", "").strip("/").partition("/")
        if not separator or not directory or bucket != os.environ.get("DEFAULT_OBJECT_STORAGE_BUCKET_ID"):
            raise ImproperlyConfigured("Private App Storage configuration does not match its bucket.")
        return f"{directory}/govbills/production"

    def object_key(self, name):
        path = PurePosixPath(str(name))
        if not name or path.is_absolute() or ".." in path.parts or "\\" in str(name):
            raise SuspiciousFileOperation("Invalid private file name.")
        return f"{self.prefix}/{path.as_posix()}"

    def _open(self, name, mode="rb"):
        if mode not in {"r", "rb"}:
            raise ValueError("Private files are read-only; use save() to write a new object.")
        return File(io.BytesIO(self.client.download_as_bytes(self.object_key(name))), name=name)

    def _save(self, name, content):
        buffer = io.BytesIO()
        limit = settings.MAX_UPLOAD_MB * 1024 * 1024
        for chunk in content.chunks():
            if buffer.tell() + len(chunk) > limit:
                raise SuspiciousFileOperation("Private file exceeds the configured upload limit.")
            buffer.write(chunk)
        self.client.upload_from_bytes(self.object_key(name), buffer.getvalue())
        return name

    def exists(self, name):
        return self.client.exists(self.object_key(name))

    def delete(self, name):
        self.client.delete(self.object_key(name), ignore_not_found=True)

    def size(self, name):
        return len(self.client.download_as_bytes(self.object_key(name)))

    def url(self, name):
        raise ValueError("Private files must be downloaded through an authorized GovBills view.")
