"""Post-settlement debit matching. Never changes invoices, payment state or bank instructions."""
import csv
import hashlib
import io
import json
from collections import Counter
from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import AuditEvent
from org.access import has_role
from org.models import Role
from reconciliation.models import BankAllocation, BankRow, BankStatement, BankTransactionKey
from reconciliation.parser import MAX_BYTES, MAX_ROWS, norm, parse_amount
from settlements.models import SettlementInstruction

MANAGERS = [Role.FINANCE, Role.UNIT_ADMIN]
READERS = MANAGERS + [Role.AUTHORISER, Role.AUDITOR]
HEADERS = ["transaction_id", "settlement_reference", "provider_code", "amount", "currency", "value_date", "bank_reference"]
REQUIRED = {"transaction_id", "settlement_reference", "amount", "currency", "value_date"}


def fingerprint(instruction):
    payload = [instruction.pk, instruction.reference, instruction.status, instruction.currency,
               format(instruction.total_amount, ".3f"), str(instruction.executed_at), instruction.bank_reference,
               list(instruction.lines.order_by("pk").values_list("pk", "provider_id", "amount", "end_to_end_id"))]
    return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode()).hexdigest()


def bank_status(instruction):
    lines = list(instruction.lines.select_related("bank_allocation__row"))
    digest = fingerprint(instruction)
    cleared = sum(1 for line in lines if hasattr(line, "bank_allocation") and
                  line.bank_allocation.row.status == BankRow.Status.MATCHED and
                  line.bank_allocation.row.fingerprint == digest)
    return {"valid": instruction.status == "executed" and bool(lines) and cleared == len(lines),
            "cleared": cleared, "total": len(lines),
            "label": "Bank reconciled" if lines and cleared == len(lines) and instruction.status == "executed" else
                     "Partially reconciled" if cleared else "Awaiting bank reconciliation"}


def parse(data):
    if len(data) > MAX_BYTES:
        raise ValidationError("Bank CSV must be at most 10 MB.")
    try:
        content = data.decode("utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(content[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(content), dialect=dialect)
        fields = [norm(f).replace(" ", "_") for f in (reader.fieldnames or [])]
        if len(fields) != len(set(fields)) or not REQUIRED.issubset(fields):
            raise ValidationError("Use the template columns: transaction_id, settlement_reference, amount, currency, value_date; optional provider_code and bank_reference.")
        reader.fieldnames = fields
        result = []
        for number, raw in enumerate(reader, 2):
            if number > MAX_ROWS + 1:
                raise ValidationError("Bank CSV may contain at most 10,000 rows.")
            if not any(raw.values()):
                continue
            if None in raw:
                raise ValidationError("A bank CSV row contains more values than its header.")
            raw = {key: str(value or "").strip() for key, value in raw.items()}
            errors = []
            amount, value_date = None, None
            try:
                amount = parse_amount(raw.get("amount", ""))
                if amount <= 0:
                    errors.append("Only positive settlement debits can auto-match; fees, credits and reversals need investigation.")
            except ValueError as exc:
                errors.append(str(exc))
            try:
                value_date = date.fromisoformat(raw.get("value_date", ""))
            except ValueError:
                errors.append("Value date must be YYYY-MM-DD.")
            if not raw.get("transaction_id") or not raw.get("settlement_reference"):
                errors.append("Transaction ID and settlement reference are required.")
            if any(len(raw.get(field, "")) > maximum for field, maximum in [
                    ("transaction_id", 120), ("settlement_reference", 80), ("provider_code", 30), ("bank_reference", 120)]):
                errors.append("A reference exceeds the permitted length.")
            values = {key: raw.get(key, "")[:maximum] for key, maximum in [
                ("transaction_id", 120), ("settlement_reference", 80), ("provider_code", 30), ("bank_reference", 120)]}
            result.append({**values, "line_number": number, "raw": raw, "amount": amount,
                           "value_date": value_date, "parse_error": " ".join(errors)})
        if not result:
            raise ValidationError("The bank CSV contains no transactions.")
        return result
    except (UnicodeError, csv.Error) as exc:
        raise ValidationError("Upload a readable UTF-8 CSV using the template.") from exc


@transaction.atomic
def recheck(row, user, accept_date=False, reason=""):
    # Permissions are rechecked after locking so stale forms cannot change scope.
    row = BankRow.objects.select_for_update().select_related("statement").get(pk=row.pk)
    statement = row.statement
    if not has_role(user, statement.org_unit, MANAGERS):
        raise PermissionDenied
    if len(reason) > 2000:
        raise ValidationError("Review reason must not exceed 2,000 characters.")
    issue = row.parse_error
    candidates = list(SettlementInstruction.objects.select_for_update().filter(
        reference__iexact=row.settlement_reference, org_unit__path__startswith=statement.org_unit.path)[:2])
    instruction = candidates[0] if len(candidates) == 1 else None
    lines = list(instruction.lines.select_related("provider")) if instruction else []
    selected = [line for line in lines if norm(line.provider.short_code) == norm(row.provider_code)] if row.provider_code else lines
    digest = fingerprint(instruction) if instruction else ""
    if row.status == BankRow.Status.MATCHED and row.fingerprint == digest:
        return row
    if row.allocations.exists():
        prior = list(row.allocations.values_list("line_id", flat=True))
        row.allocations.all().delete()
        BankTransactionKey.objects.filter(row=row).delete()
        AuditEvent.record("bank_reconciliation.stale", obj=row, actor=user,
                          org_unit_id=statement.org_unit_id, data={"previous_lines": prior})
    if not issue:
        if not instruction:
            issue = "No unique settlement exists in this statement's organisational coverage."
        elif instruction.status != "executed":
            issue = "Settlement is not recorded as executed. Bank matching does not mark it paid."
        elif not instruction.executed_at:
            issue = "The executed settlement has no recorded execution date."
        elif not lines or sum((line.amount for line in lines), Decimal("0")) != instruction.total_amount:
            issue = "Settlement total and beneficiary transfers are inconsistent."
        elif row.raw.get("currency", "").upper() != statement.currency or instruction.currency != statement.currency:
            issue = "Bank row, statement and settlement currencies must agree. No FX conversion."
        elif not selected or (row.provider_code and len(selected) != 1):
            issue = "Provider code does not identify a unique beneficiary transfer."
        elif row.amount != sum((line.amount for line in selected), Decimal("0")):
            issue = (f"Bank debit amount {row.amount} {statement.currency} differs from the executed "
                     f"transfer total {sum((line.amount for line in selected), Decimal('0'))} {instruction.currency}.")
        elif row.bank_reference and norm(row.bank_reference) != norm(instruction.bank_reference):
            issue = "Bank reference differs from the recorded execution reference."
        elif not row.value_date or row.value_date > statement.statement_date or row.value_date > timezone.localdate():
            issue = "Bank value date is after the statement date or in the future."
    transaction_key = hashlib.sha256((norm(statement.account_label) + "|" + norm(row.transaction_id)).encode()).hexdigest()
    existing = BankTransactionKey.objects.select_related("row__statement").filter(pk=transaction_key).first() if not issue else None
    duplicate = False
    if existing and existing.row_id != row.pk:
        previous = existing.row
        duplicate = (norm(previous.settlement_reference) == norm(row.settlement_reference) and
                     norm(previous.provider_code) == norm(row.provider_code) and previous.amount == row.amount and
                     previous.value_date == row.value_date and previous.raw.get("currency") == row.raw.get("currency") and
                     previous.statement.currency == statement.currency and
                     norm(previous.bank_reference) == norm(row.bank_reference) and not row.parse_error)
        issue = "This bank transaction was already reconciled from another upload." if duplicate else \
                "Conflicting replay: this bank transaction ID was already allocated to a different payment."
    if not issue and row.value_date != instruction.executed_at:
        if row.reviewed_fingerprint == digest and row.reviewed_by_id:
            pass
        elif accept_date and reason.strip() and user.pk != statement.uploaded_by_id:
            row.reviewed_by, row.review_reason, row.reviewed_fingerprint = user, reason.strip(), digest
        else:
            issue = "Value date differs from recorded settlement execution. An independent finance/admin reviewer must document a timing difference."
    if not issue and BankAllocation.objects.filter(line__in=selected).exclude(row=row).exists():
        issue = "A beneficiary transfer is already bank reconciled; duplicate allocation is blocked."
    row.instruction, row.fingerprint = instruction, digest if not issue else ""
    row.status = BankRow.Status.DUPLICATE if duplicate else BankRow.Status.EXCEPTION if issue else BankRow.Status.MATCHED
    row.reason = issue or "Bank debit, reference, currency, beneficiary coverage and value date verified."
    row.checked_by, row.checked_at = user, timezone.now()
    if not issue:
        key, created = BankTransactionKey.objects.get_or_create(key=transaction_key, defaults={"row": row})
        if not created and key.row_id != row.pk:
            raise ValidationError("A concurrent upload used this bank transaction. Recheck the line.")
        for line in selected:
            BankAllocation.objects.create(line=line, row=row)
    row.save()
    AuditEvent.record("bank_reconciliation.checked", obj=row, actor=user, org_unit_id=statement.org_unit_id,
                      data={"status": row.status, "reason": row.reason, "instruction": instruction.pk if instruction else None,
                            "timing_review": row.review_reason if not issue else ""})
    return row


@transaction.atomic
def upload(user, org_unit, account_label, currency, statement_date, document):
    if not has_role(user, org_unit, MANAGERS):
        raise PermissionDenied
    if statement_date > timezone.localdate():
        raise ValidationError("Statement date cannot be in the future.")
    if not document.name.lower().endswith(".csv"):
        raise ValidationError("Upload a UTF-8 bank CSV; use the downloadable template.")
    data = document.read(MAX_BYTES + 1)
    values = parse(data)
    statement = BankStatement.objects.create(org_unit=org_unit, account_label=account_label.strip(),
        currency=currency, statement_date=statement_date, uploaded_by=user, filename=document.name[:255],
        sha256=hashlib.sha256(data).hexdigest())
    statement.document.save(document.name, ContentFile(data))
    # Acquire all batch targets in one order before any per-row audit writes.
    # This avoids holding the audit-chain lock while waiting on a later batch target.
    references = {norm(value["settlement_reference"]) for value in values}
    list(SettlementInstruction.objects.select_for_update().filter(
        org_unit__path__startswith=org_unit.path).alias(bank_ref=Lower("reference")).filter(
            bank_ref__in=references).order_by("pk"))
    for value in values:
        BankRow.objects.create(statement=statement, **value)
    frequencies = Counter(norm(value["transaction_id"]) for value in values)
    duplicate_ids = {value["transaction_id"] for value in values if frequencies[norm(value["transaction_id"])] > 1}
    for row in statement.rows.all():
        if row.transaction_id in duplicate_ids:
            row.parse_error = "Duplicate transaction ID inside this bank file. Neither entry may be allocated."
            row.save(update_fields=["parse_error"])
        recheck(row, user)
    AuditEvent.record("bank_reconciliation.uploaded", obj=statement, actor=user, org_unit_id=org_unit.pk,
                      data={"sha256": statement.sha256, "rows": len(values)})
    return statement
