"""Upload validation by content (magic bytes), not by the client-supplied name or type."""

import hashlib
import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

SIGNATURES = {
    "pdf": [b"%PDF-"],
    "xlsx": [b"PK\x03\x04"],
    "xls": [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "tif": [b"II*\x00", b"MM\x00*"],
}
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "png": "image/png",
    "jpg": "image/jpeg",
    "tif": "image/tiff",
}


def detect_kind(head: bytes, name: str):
    for kind, magics in SIGNATURES.items():
        if any(head.startswith(m) for m in magics):
            if kind == "xlsx" and not name.lower().endswith(".xlsx"):
                return None  # any zip file is not a spreadsheet
            return kind
    if name.lower().endswith(".csv"):
        try:
            head.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
        if b"\x00" in head:
            return None
        return "csv"
    return None


def validate_upload(uploaded, allowed):
    """Validate size and content; return (kind, sha256_hex)."""
    if uploaded.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f"File is larger than {settings.MAX_UPLOAD_MB} MB.")
    uploaded.seek(0)
    head = uploaded.read(4096)
    kind = detect_kind(head, uploaded.name or "")
    if kind not in allowed:
        raise ValidationError("Unsupported file. Allowed: " + ", ".join(sorted(allowed)).upper() + ".")
    if kind == "xlsx":
        import zipfile

        uploaded.seek(0)
        try:
            with zipfile.ZipFile(uploaded) as zf:
                if "[Content_Types].xml" not in zf.namelist():
                    raise ValidationError("The file is not a valid Excel workbook.")
                if sum(i.file_size for i in zf.infolist()) > 200 * 1024 * 1024:
                    raise ValidationError("The workbook expands to an unsafe size.")
        except zipfile.BadZipFile:
            raise ValidationError("The file is not a valid Excel workbook.")
    digest = hashlib.sha256()
    uploaded.seek(0)
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    return kind, digest.hexdigest()


@deconstructible
class PrivateUploadPath:
    """upload_to: random names so stored paths reveal nothing about the content."""

    def __init__(self, prefix):
        self.prefix = prefix

    def __call__(self, instance, filename):
        ext = os.path.splitext(filename)[1].lower()[:8]
        return f"{self.prefix}/{uuid.uuid4().hex}{ext}"

    def __eq__(self, other):
        return isinstance(other, PrivateUploadPath) and other.prefix == self.prefix


def private_upload_path(prefix):
    return PrivateUploadPath(prefix)


def sha256_of_field(field_file):
    digest = hashlib.sha256()
    field_file.open("rb")
    try:
        for chunk in field_file.chunks():
            digest.update(chunk)
    finally:
        field_file.close()
    return digest.hexdigest()
