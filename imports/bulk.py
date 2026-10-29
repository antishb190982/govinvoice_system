"""Bounded folder/ZIP intake. Never extract archive paths onto the filesystem."""

import json
import stat
import zipfile
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from core.files import validate_upload
from imports import services

DOCUMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
SHEET_EXTENSIONS = {".xlsx", ".xls", ".csv"}
SUPPORTED = DOCUMENT_EXTENSIONS | SHEET_EXTENSIONS
MAX_FILES = 100
MAX_TOTAL_BYTES = 100 * 1024 * 1024


def safe_path(name):
    name = str(name)
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or "\x00" in name:
        raise ValidationError("Unsafe file path in the selected folder or ZIP.")
    return path


def apply_folder_paths(files, raw_paths):
    try:
        paths = json.loads(raw_paths) if raw_paths else [f.name for f in files]
        if not isinstance(paths, list) or len(paths) != len(files):
            raise ValueError
        for file, name in zip(files, paths):
            path = safe_path(name)
            if path.name != file.name:
                raise ValueError
            file.intake_path = str(path)
    except (ValueError, TypeError):
        raise ValidationError("Folder file names did not match the uploaded files.") from None


def expand_uploads(files):
    expanded, skipped = [], []
    budget = 0
    if not files:
        raise ValidationError("Choose files, a folder, or a ZIP archive.")
    if len(files) > MAX_FILES or sum(f.size for f in files) > MAX_TOTAL_BYTES:
        raise ValidationError("Upload at most 100 files / 100 MB per intake.")
    for uploaded in files:
        path = safe_path(getattr(uploaded, "intake_path", uploaded.name))
        if path.suffix.lower() != ".zip":
            if path.suffix.lower() not in SUPPORTED:
                skipped.append(f"{path}: unsupported file type")
                continue
            budget += uploaded.size
            expanded.append(uploaded)
            continue
        if uploaded.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
            raise ValidationError(f"A ZIP archive must not exceed {settings.MAX_UPLOAD_MB} MB.")
        try:
            with zipfile.ZipFile(uploaded) as archive:
                entries = archive.infolist()
                if len(entries) > 250:
                    raise ValidationError("The ZIP has too many entries (maximum 250, including folders).")
                if sum(entry.file_size for entry in entries) + budget > MAX_TOTAL_BYTES:
                    raise ValidationError("The ZIP expands beyond the 100 MB intake limit.")
                for entry in entries:
                    name = safe_path(entry.filename)
                    if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                        raise ValidationError("Encrypted ZIPs and symbolic links are not accepted.")
                    if entry.is_dir():
                        continue
                    if len(expanded) >= MAX_FILES:
                        raise ValidationError("Upload at most 100 invoice files per intake.")
                    if entry.file_size > settings.MAX_UPLOAD_MB * 1024 * 1024:
                        raise ValidationError(f"{name.name}: exceeds the per-file upload limit.")
                    if name.suffix.lower() not in SUPPORTED or "__MACOSX" in name.parts:
                        skipped.append(f"{path.name}/{name}: unsupported file type (nested ZIPs are not opened)")
                        continue
                    content = archive.read(entry)
                    budget += len(content)
                    file = SimpleUploadedFile(name.name, content)
                    file.intake_path = f"{path.name}/{name}"
                    expanded.append(file)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError):
            raise ValidationError("The ZIP could not be read. Check that it is complete and not encrypted.") from None
        finally:
            uploaded.seek(0)
    if budget > MAX_TOTAL_BYTES:
        raise ValidationError("The expanded files exceed the 100 MB intake limit.")
    if not expanded:
        raise ValidationError("No supported invoice files were found in the selection.")
    if len(expanded) > MAX_FILES:
        raise ValidationError("Upload at most 100 invoice files per intake.")
    return expanded, skipped


@transaction.atomic
def stage_uploads(user, files, provider=None, *, received_date=None, intake_source="upload"):
    expanded, skipped = expand_uploads(files)
    # Validate every file's content before creating batches, not just extensions.
    documents, sheets = [], []
    for file in expanded:
        kind, _ = validate_upload(file, services.DOCUMENT_KINDS | {"xlsx", "xls", "csv"})
        (documents if kind in services.DOCUMENT_KINDS else sheets).append(file)
    kwargs = {"received_date": received_date, "intake_source": intake_source}
    batches = [services.stage_documents(user, documents, provider, **kwargs)] if documents else []
    for sheet in sheets:
        batches.append(services.stage_spreadsheet(user, sheet, provider, **kwargs))
        if sum(batch.rows.count() for batch in batches) > 5000:
            raise ValidationError("The combined intake must not exceed 5,000 invoice rows.")
    for batch in batches:
        batch.intake_notes = skipped
        batch.save(update_fields=["intake_notes"])
    return batches, skipped
