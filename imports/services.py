from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from core.files import validate_upload
from core.models import AuditEvent
from imports import parsers
from imports.models import ImportBatch, ImportRow, ImportTemplate
from invoices.models import Invoice
from invoices.services import save_invoice
from org.access import has_role
from org.models import Role
from providers.models import Provider, ServiceAccount


def _template_for(provider, kind):
    if not provider:
        return None
    return ImportTemplate.objects.filter(provider=provider, kind=kind, is_active=True).first()


def _resolve_provider(data, default):
    from providers.services import provider_from_invoice_number

    code = data.get("provider")
    if code:
        found = Provider.objects.filter(short_code__iexact=code).first() or \
            Provider.objects.filter(name__iexact=code).first()
        if found:
            return found
    return default or provider_from_invoice_number(data.get("invoice_number"))


def validate_row(row, user, default_provider, template=None):
    data, errors = parsers.normalise(row.raw, template.date_format if template else None,
                                     template.decimal_comma if template else False)
    provider = _resolve_provider(data, default_provider)
    account = None
    if provider is None:
        errors.append("provider not identified – choose one when uploading")
    elif data.get("account_number") or data.get("service_number"):
        from providers.services import find_account

        account = find_account(provider, data.get("account_number"), data.get("service_number"))
        if account is None:
            number = data.get("account_number") or data.get("service_number")
            errors.append(f"account {number} is not registered for {provider.short_code}")
            data["unregistered_account"] = number
        elif not has_role(user, account.org_unit, [Role.CLERK, Role.UNIT_ADMIN]):
            errors.append(f"you may not record invoices for {account.org_unit.code}")
        else:
            data["account_number"] = account.account_number
    if provider and data.get("invoice_number") and Invoice.objects.filter(
            provider=provider, invoice_number__iexact=data["invoice_number"]).exists():
        errors.append(f"invoice {data['invoice_number']} is already recorded")
    if data.get("issue_date") and data.get("due_date") and data["due_date"] < data["issue_date"]:
        errors.append("due date is before issue date")
    notes = []
    if data.get("due_date") and date.fromisoformat(data["due_date"]) < timezone.localdate():
        notes.append("already past due")  # not an error – it must still be paid
    if data.get("amount_total") is not None and data.get("amount_total") != "" and Decimal(data["amount_total"]) == 0:
        notes.append("nothing to pay")
    if data.get("arrears") and Decimal(data["arrears"]) > 0:
        notes.append(f"arrears {Decimal(data['arrears']):,.2f} from earlier bills")
    if notes:
        data["warning"] = "; ".join(notes)
    if provider:
        data["provider_id"] = provider.pk
        data["provider_code"] = provider.short_code
    if account:
        data["service_account_id"] = account.pk
        data["org_unit_code"] = account.org_unit.code
    row.data = data
    row.errors = errors
    row.status = ImportRow.Status.INVALID if errors else ImportRow.Status.VALID
    return row


def _mark_batch_duplicates(batch):
    seen = {}
    for row in batch.rows.all():
        key = (row.data.get("provider_id"), (row.data.get("invoice_number") or "").lower())
        if key[1] and key in seen:
            row.errors = row.errors + [f"duplicate of row {seen[key]} in this file"]
            row.status = ImportRow.Status.INVALID
            row.save(update_fields=["errors", "status"])
        else:
            seen[key] = row.row_number


@transaction.atomic
def stage_spreadsheet(user, uploaded, provider=None):
    kind, digest = validate_upload(uploaded, {"xlsx", "xls", "csv"})
    template = _template_for(provider, ImportTemplate.Kind.SPREADSHEET)
    content = uploaded.read()
    try:
        rows = parsers.spreadsheet_rows(content, kind, template)
    except Exception as exc:  # parser errors are shown to the user
        raise ValidationError(f"Could not read the file: {exc}")
    if not rows:
        raise ValidationError("The file contains no invoice rows.")
    if len(rows) > 5000:
        raise ValidationError("Import at most 5,000 rows at a time.")
    batch = ImportBatch(uploaded_by=user, provider=provider, template=template, kind=kind,
                        original_name=uploaded.name[:255], sha256=digest)
    batch.file.save(uploaded.name, ContentFile(content), save=False)
    batch.save()
    for number, raw in rows:
        row = ImportRow(batch=batch, row_number=number, raw={k: (str(v) if v is not None else None) for k, v in raw.items()})
        validate_row(row, user, provider, template)
        row.save()
    _mark_batch_duplicates(batch)
    AuditEvent.record("import.staged", obj=batch, actor=user, data={"file": batch.original_name, "sha256": digest,
                                                                     "rows": len(rows)})
    return batch


DOCUMENT_KINDS = {"pdf", "png", "jpg", "tif"}


def stage_documents(user, files, provider=None):
    """Stage PDF and image invoices. Each file becomes an OCR document and one row;
    small uploads are analysed immediately, larger ones by the background worker."""
    from django.conf import settings

    batch = _stage_documents(user, files, provider)
    if len(files) <= settings.OCR_SYNC_MAX_FILES:
        for row in batch.rows.filter(status=ImportRow.Status.ANALYSING).select_related("ocr_document"):
            apply_ocr(row)
    return batch


@transaction.atomic
def _stage_documents(user, files, provider):
    from ocr import services as ocr

    if not files:
        raise ValidationError("Choose at least one file.")
    if len(files) > 100:
        raise ValidationError("Upload at most 100 documents at a time.")
    checked = []
    for f in files:
        kind, digest = validate_upload(f, DOCUMENT_KINDS)
        f.seek(0)
        checked.append((f, kind, digest, f.read()))
    first = checked[0]
    batch = ImportBatch(uploaded_by=user, provider=provider, kind="documents",
                        original_name=(first[0].name if len(files) == 1 else f"{len(files)} scanned / PDF invoices")[:255],
                        sha256=first[2])
    batch.file.save(first[0].name, ContentFile(first[3]), save=False)
    batch.save()
    for number, (f, kind, digest, content) in enumerate(checked, start=1):
        row = ImportRow(batch=batch, row_number=number, source_name=f.name[:255], source_sha256=digest,
                        status=ImportRow.Status.ANALYSING)
        row.source_file.save(f.name, ContentFile(content), save=False)
        row.ocr_document = ocr.create_document(user, content, f.name, kind)
        row.save()
    AuditEvent.record("import.staged", obj=batch, actor=user, data={"files": len(files), "kind": "documents"})
    return batch


# Former name, kept for callers that only send PDFs.
stage_pdfs = stage_documents


def apply_ocr(row):
    """Analyse a row's document and validate the extracted values."""
    from ocr import services as ocr
    from ocr.engine import DocumentError

    batch = row.batch
    doc = row.ocr_document
    try:
        warnings = ocr.analyse(doc, hint=batch.provider)
    except DocumentError as exc:
        row.raw, row.data, row.errors, row.status = {}, {}, [str(exc)], ImportRow.Status.INVALID
        row.save()
        return row
    except Exception as exc:  # engine failure: surface, don't crash the batch
        row.raw, row.data, row.errors, row.status = {}, {}, [f"Analysis failed: {exc.__class__.__name__}"], ImportRow.Status.INVALID
        row.save()
        return row
    row.raw = {k: v for k, v in ocr.as_import_raw(doc).items() if k in parsers.FIELDS}
    row.confidence = ocr.confidences(doc)
    row.warnings = warnings
    row.needs_review = ocr.needs_review(doc) or bool(warnings)
    validate_row(row, batch.uploaded_by, batch.provider, batch.template)
    if Invoice.objects.filter(document_sha256=row.source_sha256).exists():
        row.errors = row.errors + ["this exact document is already attached to an invoice"]
        row.status = ImportRow.Status.INVALID
    row.save()
    _mark_batch_duplicates(batch)
    return row


def confirm_row(row, user, values, learn=True):
    """A person checked the extraction against the document: re-validate, mark
    confirmed and (optionally) teach the provider template where the values are."""
    from ocr import services as ocr
    from providers.models import Provider

    update_row(row, user, values)
    if row.status != ImportRow.Status.VALID:
        return row, []
    row.needs_review = False
    row.confirmed_by = user
    row.confirmed_at = timezone.now()
    row.save(update_fields=["needs_review", "confirmed_by", "confirmed_at"])
    learned = []
    if learn and row.ocr_document and row.data.get("provider_id"):
        provider = Provider.objects.get(pk=row.data["provider_id"])
        confirmed = {f: row.data.get(f) for f in parsers.FIELDS if row.data.get(f) not in (None, "")}
        learned = ocr.learn(row.ocr_document, provider, confirmed, user)
    AuditEvent.record("import.row_confirmed", obj=row.batch, actor=user,
                      data={"row": row.row_number, "learned": learned})
    return row, learned


def recheck(batch, user):
    """Validate staged rows again, e.g. after the missing service accounts were registered."""
    if batch.status != ImportBatch.Status.STAGED or batch.uploaded_by_id != user.pk:
        raise PermissionDenied
    count = 0
    for row in batch.rows.exclude(status__in=[ImportRow.Status.COMMITTED, ImportRow.Status.ANALYSING]):
        if row.ocr_document and not row.raw:
            continue  # unreadable document: nothing to validate
        errors_before = list(row.errors)
        validate_row(row, user, batch.provider, batch.template)
        if row.source_sha256 and Invoice.objects.filter(document_sha256=row.source_sha256).exists():
            row.errors = row.errors + ["this exact document is already attached to an invoice"]
            row.status = ImportRow.Status.INVALID
        row.save()
        count += row.errors != errors_before
    _mark_batch_duplicates(batch)
    return count


def update_row(row, user, values):
    """Correct a staged row from the review screen, then re-validate it."""
    if row.batch.status != ImportBatch.Status.STAGED or row.batch.uploaded_by_id != user.pk:
        raise PermissionDenied
    raw = dict(row.raw)
    for field in parsers.FIELDS:
        if field in values:
            raw[field] = values[field]
    row.raw = raw
    validate_row(row, user, row.batch.provider, row.batch.template)
    row.save()
    return row


def commit(batch, user):
    if batch.status != ImportBatch.Status.STAGED:
        raise ValidationError("This import was already processed.")
    if batch.uploaded_by_id != user.pk:
        raise PermissionDenied("Only the person who uploaded the file can commit it.")
    created, failed, unconfirmed = [], 0, 0
    for row in batch.rows.filter(status=ImportRow.Status.VALID, include=True):
        if row.needs_review and not row.confirmed_at:
            unconfirmed += 1
            continue
        validate_row(row, user, batch.provider, batch.template)  # state may have changed since staging
        if row.status != ImportRow.Status.VALID:
            row.save()
            failed += 1
            continue
        d = row.data
        account = ServiceAccount.objects.get(pk=d["service_account_id"])
        total = Decimal(d["amount_total"])
        tax = Decimal(d["amount_tax"]) if d.get("amount_tax") else Decimal("0")
        # Statements often give only the total incl. VAT and the VAT: derive the net.
        net = Decimal(d["amount_net"]) if d.get("amount_net") else (total - tax if d.get("amount_tax") else Decimal("0"))
        inv = Invoice(
            service_account=account, invoice_number=d["invoice_number"],
            issue_date=date.fromisoformat(d["issue_date"]), due_date=date.fromisoformat(d["due_date"]),
            period_start=date.fromisoformat(d["period_start"]) if d.get("period_start") else None,
            period_end=date.fromisoformat(d["period_end"]) if d.get("period_end") else None,
            amount_net=net, amount_tax=tax, amount_total=total,
            arrears=Decimal(d["arrears"]) if d.get("arrears") else None,
            consumption=Decimal(d["consumption"]) if d.get("consumption") else None,
            previous_reading=Decimal(d["previous_reading"]) if d.get("previous_reading") else None,
            current_reading=Decimal(d["current_reading"]) if d.get("current_reading") else None,
            currency=(d.get("currency") or "").upper()[:3],
            source=(Invoice.Source.OCR if row.ocr_document and row.ocr_document.used_ocr else Invoice.Source.PDF)
            if row.source_file else Invoice.Source.SPREADSHEET,
            import_batch=batch, ocr_document=row.ocr_document,
        )
        source = row.source_file if row.source_file else batch.file
        source.open("rb")
        content = source.read()
        source.close()
        inv.document.save(row.source_name or batch.original_name, ContentFile(content), save=False)
        inv.document_name = row.source_name or batch.original_name
        inv.document_sha256 = row.source_sha256 or batch.sha256
        try:
            with transaction.atomic():
                save_invoice(inv, user, audit_action="invoice.imported")
        except ValidationError as exc:
            inv.document.delete(save=False)
            row.errors = [m for msgs in getattr(exc, "message_dict", {"": exc.messages}).values() for m in msgs]
            row.status = ImportRow.Status.INVALID
            row.save()
            failed += 1
            continue
        row.invoice = inv
        row.status = ImportRow.Status.COMMITTED
        row.save()
        created.append(inv)
    pending = unconfirmed + batch.rows.filter(status=ImportRow.Status.ANALYSING).count()
    if not pending:
        batch.rows.filter(include=False).update(status=ImportRow.Status.SKIPPED)
        batch.status = ImportBatch.Status.COMMITTED
        batch.committed_at = timezone.now()
        batch.save()
    AuditEvent.record("import.committed", obj=batch, actor=user,
                      data={"created": len(created), "failed": failed, "awaiting_confirmation": unconfirmed})
    return created, failed
