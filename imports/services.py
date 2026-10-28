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
    code = data.get("provider")
    if code:
        found = Provider.objects.filter(short_code__iexact=code).first() or \
            Provider.objects.filter(name__iexact=code).first()
        if found:
            return found
    return default


def validate_row(row, user, default_provider, template=None):
    data, errors = parsers.normalise(row.raw, template.date_format if template else None,
                                     template.decimal_comma if template else False)
    provider = _resolve_provider(data, default_provider)
    account = None
    if provider is None:
        errors.append("provider not identified – choose one when uploading")
    elif data.get("account_number"):
        account = ServiceAccount.objects.filter(provider=provider, account_number__iexact=data["account_number"],
                                                is_active=True).select_related("org_unit").first()
        if account is None:
            errors.append(f"account {data['account_number']} is not registered for {provider.short_code}")
        elif not has_role(user, account.org_unit, [Role.CLERK, Role.UNIT_ADMIN]):
            errors.append(f"you may not record invoices for {account.org_unit.code}")
    if provider and data.get("invoice_number") and Invoice.objects.filter(
            provider=provider, invoice_number__iexact=data["invoice_number"]).exists():
        errors.append(f"invoice {data['invoice_number']} is already recorded")
    if data.get("issue_date") and data.get("due_date") and data["due_date"] < data["issue_date"]:
        errors.append("due date is before issue date")
    if data.get("due_date") and date.fromisoformat(data["due_date"]) < timezone.localdate():
        # Not an error – it must still be paid – but it is surfaced prominently.
        data["warning"] = "already past due"
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


@transaction.atomic
def stage_pdfs(user, files, provider=None):
    if not files:
        raise ValidationError("Choose at least one PDF.")
    if len(files) > 100:
        raise ValidationError("Upload at most 100 PDFs at a time.")
    template = _template_for(provider, ImportTemplate.Kind.PDF)
    checked = []
    for f in files:
        kind, digest = validate_upload(f, {"pdf"})
        checked.append((f, digest))
    first = checked[0][0]
    batch = ImportBatch(uploaded_by=user, provider=provider, template=template, kind="pdf",
                        original_name=(first.name if len(files) == 1 else f"{len(files)} PDF invoices")[:255],
                        sha256=checked[0][1])
    first.seek(0)
    batch.file.save(first.name, ContentFile(first.read()), save=False)
    batch.save()
    for number, (f, digest) in enumerate(checked, start=1):
        f.seek(0)
        content = f.read()
        row = ImportRow(batch=batch, row_number=number, source_name=f.name[:255], source_sha256=digest)
        row.source_file.save(f.name, ContentFile(content), save=False)
        try:
            raw, _text = parsers.pdf_row(content, template)
            row.raw = raw
            validate_row(row, user, provider, template)
        except Exception as exc:
            row.raw, row.data, row.errors, row.status = {}, {}, [str(exc)], ImportRow.Status.INVALID
        if Invoice.objects.filter(document_sha256=digest).exists():
            row.errors = row.errors + ["this exact PDF is already attached to an invoice"]
            row.status = ImportRow.Status.INVALID
        row.save()
    _mark_batch_duplicates(batch)
    AuditEvent.record("import.staged", obj=batch, actor=user, data={"files": len(files), "kind": "pdf"})
    return batch


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
    created, failed = [], 0
    for row in batch.rows.filter(status=ImportRow.Status.VALID, include=True):
        validate_row(row, user, batch.provider, batch.template)  # state may have changed since staging
        if row.status != ImportRow.Status.VALID:
            row.save()
            failed += 1
            continue
        d = row.data
        account = ServiceAccount.objects.get(pk=d["service_account_id"])
        inv = Invoice(
            service_account=account, invoice_number=d["invoice_number"],
            issue_date=date.fromisoformat(d["issue_date"]), due_date=date.fromisoformat(d["due_date"]),
            period_start=date.fromisoformat(d["period_start"]) if d.get("period_start") else None,
            period_end=date.fromisoformat(d["period_end"]) if d.get("period_end") else None,
            amount_net=Decimal(d["amount_net"]) if d.get("amount_net") else Decimal("0"),
            amount_tax=Decimal(d["amount_tax"]) if d.get("amount_tax") else Decimal("0"),
            amount_total=Decimal(d["amount_total"]),
            consumption=Decimal(d["consumption"]) if d.get("consumption") else None,
            previous_reading=Decimal(d["previous_reading"]) if d.get("previous_reading") else None,
            current_reading=Decimal(d["current_reading"]) if d.get("current_reading") else None,
            currency=(d.get("currency") or "").upper()[:3],
            source=Invoice.Source.PDF if batch.kind == "pdf" else Invoice.Source.SPREADSHEET,
            import_batch=batch,
        )
        source = row.source_file if batch.kind == "pdf" else batch.file
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
    batch.rows.filter(include=False).update(status=ImportRow.Status.SKIPPED)
    batch.status = ImportBatch.Status.COMMITTED
    batch.committed_at = timezone.now()
    batch.save()
    AuditEvent.record("import.committed", obj=batch, actor=user, data={"created": len(created), "failed": failed})
    return created, failed
