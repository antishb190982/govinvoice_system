import hashlib
import json
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, transaction
from django.db.models import Max
from django.utils import timezone

from core.models import AuditEvent
from invoices.models import Invoice
from org.access import has_role
from org.models import Role
from providers.models import Provider
from reconciliation.models import PaymentEvidence, Statement, StatementRow
from reconciliation.parser import norm, parse_statement

MANAGERS = [Role.FINANCE, Role.UNIT_ADMIN]


def lock_provider(pk):
    # Invoice decisions already hold invoice locks; source imports also write
    # invoice FKs. Fail closed rather than wait into a provider/invoice deadlock.
    try:
        with transaction.atomic():
            return Provider.objects.select_for_update(nowait=True).get(pk=pk)
    except OperationalError as exc:
        cause = exc.__cause__
        if (getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)) == "55P03":
            raise ValidationError("This provider is being reconciled or processed by another user. Retry shortly.") from exc
        raise


def fingerprint(invoice):
    data = invoice.snapshot()
    for key in ["amount_net", "amount_tax", "amount_total", "consumption", "arrears"]:
        if data[key] is not None:
            data[key] = format(Decimal(data[key]), ".3f")
    data.update(provider_id=invoice.provider_id, account_id=invoice.service_account_id, unit_id=invoice.org_unit_id)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def latest_statement(unit, provider_id, currency):
    return Statement.objects.filter(is_active=True, org_unit_id__in=unit.ancestor_ids,
                                     provider_id=provider_id, currency=currency).order_by("-statement_date", "-pk").first()


def report(invoice, lock=False):
    if lock:
        lock_provider(invoice.provider_id)
    statement = latest_statement(invoice.org_unit, invoice.provider_id, invoice.currency)
    result = {"valid": False, "statement": statement, "row": None}
    if not statement:
        return {**result, "reason": "No current provider statement covers this invoice's unit and currency."}
    rows = list(statement.rows.filter(invoice=invoice, status=StatementRow.Status.MATCHED))
    if len(rows) != 1:
        exception = statement.rows.filter(invoice=invoice, status=StatementRow.Status.UNSORTED).first()
        return {**result, "row": exception,
                "reason": exception.reason if exception and exception.reason else
                "Invoice is absent from the current reconciled statement, or has an unresolved mismatch."}
    row = rows[0]
    result["row"] = row
    if (row.fingerprint != fingerprint(invoice) or row.amount != invoice.amount_total or
            norm(row.account_number) != norm(invoice.service_account.account_number) or
            norm(row.invoice_number) != norm(invoice.invoice_number)):
        return {**result, "reason": "Invoice details changed after reconciliation. Recheck against the current provider statement."}
    from operations.services import require_clear
    try:
        require_clear(invoice)
    except ValidationError as exc:
        return {**result, "reason": " ".join(exc.messages)}
    return {**result, "valid": True, "reason": "Account, invoice and amount reconciled against the current provider statement."}


def require_reconciled(invoice, lock=True):
    result = report(invoice, lock=lock)
    if not result["valid"]:
        raise ValidationError(f"{invoice.reference}: reconciliation required. {result['reason']}")
    return result["row"]


@transaction.atomic
def recheck(row, user, auto_route=False):
    if not has_role(user, row.assigned_unit, MANAGERS + [Role.CLERK]):
        raise PermissionDenied
    lock_provider(row.statement.provider_id)
    row = StatementRow.objects.select_for_update().select_related("statement", "assigned_unit").get(pk=row.pk)
    if not has_role(user, row.assigned_unit, MANAGERS + [Role.CLERK]):
        raise PermissionDenied
    reason, candidate = row.parse_error, None
    duplicates = row.statement.rows.filter(invoice_number__iexact=row.invoice_number).exclude(pk=row.pk).exists()
    candidates = Invoice.objects.filter(provider=row.statement.provider,
        invoice_number__iexact=row.invoice_number, org_unit__path__startswith=row.assigned_unit.path
    ).select_related("service_account", "org_unit", "provider")
    matches = list(candidates[:2])
    if not reason and duplicates:
        reason = "Duplicate invoice reference in this statement. Upload a corrected statement; neither duplicate is eligible."
    if not reason and len(matches) != 1:
        reason = "No unique recorded invoice in the assigned unit. Capture/register the invoice or correct its unit assignment."
    if len(matches) == 1:
        candidate = matches[0]
        if auto_route:
            row.assigned_unit = candidate.org_unit
    current = latest_statement(candidate.org_unit if candidate else row.assigned_unit,
                               row.statement.provider_id, row.statement.currency)
    superseded = not row.statement.is_active or (current and current.pk != row.statement_id)
    closed_ledger_gap = row.is_ledger_only and candidate and candidate.status in [Invoice.Status.PAID, Invoice.Status.CANCELLED]
    superseded = superseded or closed_ledger_gap
    if not reason and candidate:
        if candidate.status in [Invoice.Status.PAID, Invoice.Status.CANCELLED]:
            reason = "Provider lists an invoice already paid or cancelled. Investigate with the provider; do not pay twice."
        elif norm(candidate.service_account.account_number) != norm(row.account_number):
            reason = "Service-account number does not match the recorded invoice."
        elif candidate.currency != row.statement.currency:
            reason = "Recorded invoice and statement currencies differ."
        elif candidate.issue_date > row.statement.statement_date:
            reason = "Invoice issue date is later than the provider statement date."
        elif candidate.amount_total != row.amount:
            reason = f"Amount mismatch: statement {row.amount}; recorded invoice {candidate.amount_total}."
    row.invoice = candidate
    row.status = StatementRow.Status.SUPERSEDED if superseded else \
        StatementRow.Status.UNSORTED if reason else StatementRow.Status.MATCHED
    row.reason = "The ledger-only exception is closed: the invoice is now paid or cancelled." if closed_ledger_gap else \
        "A newer statement covers this invoice or assigned unit." if superseded else reason
    row.fingerprint = fingerprint(candidate) if row.status == StatementRow.Status.MATCHED else ""
    row.checked_by, row.checked_at = user, timezone.now()
    row.save()
    AuditEvent.record("reconciliation.checked", obj=row, actor=user, org_unit_id=row.assigned_unit_id,
                      data={"state": row.status, "reason": row.reason, "invoice": candidate.pk if candidate else None})
    return row


@transaction.atomic
def upload_statement(user, provider, org_unit, currency, statement_date, document):
    if not has_role(user, org_unit, MANAGERS):
        raise PermissionDenied
    if statement_date > timezone.localdate():
        raise ValidationError("A provider statement cannot be dated in the future.")
    data = document.read(10 * 1024 * 1024 + 1)
    parsed = parse_statement(data, document.name, currency)
    lock_provider(provider.pk)
    existing = Statement.objects.filter(provider=provider, org_unit=org_unit, currency=currency, is_active=True)
    if existing.filter(statement_date__gt=statement_date).exists():
        raise ValidationError("An older statement cannot replace a newer current statement for this unit.")
    Statement.objects.filter(provider=provider, currency=currency, is_active=True,
        org_unit__path__startswith=org_unit.path, statement_date__lte=statement_date).update(is_active=False)
    statement = Statement.objects.create(provider=provider, org_unit=org_unit, currency=currency,
        statement_date=statement_date, filename=document.name[:255], sha256=hashlib.sha256(data).hexdigest(), uploaded_by=user)
    from django.core.files.base import ContentFile
    statement.document.save(document.name, ContentFile(data))
    for values in parsed:
        StatementRow.objects.create(statement=statement, assigned_unit=org_unit, **values)
    for row in statement.rows.select_related("statement", "assigned_unit"):
        recheck(row, user, auto_route=True)
    for invoice in Invoice.objects.filter(provider=provider, currency=currency,
            org_unit__path__startswith=org_unit.path, status__in=Invoice.OPEN).select_related(
                "org_unit", "provider", "service_account"):
        ensure_missing_line(invoice, statement)
    AuditEvent.record("reconciliation.statement_uploaded", obj=statement, actor=user, org_unit_id=org_unit.pk,
                      data={"rows": len(parsed), "sha256": statement.sha256, "currency": currency})
    return statement


@transaction.atomic
def assign(row, user, unit, assignee, reason):
    if not has_role(user, row.assigned_unit, MANAGERS) or not has_role(user, unit, MANAGERS):
        raise PermissionDenied
    if not unit.path.startswith(row.statement.org_unit.path) or not reason.strip():
        raise ValidationError("Assign within the statement's unit coverage and give a reason.")
    if assignee and (not assignee.is_active or not has_role(assignee, unit, MANAGERS + [Role.CLERK])):
        raise ValidationError("The assignee needs an active clerk, finance or unit-administrator role in that unit.")
    lock_provider(row.statement.provider_id)
    row = StatementRow.objects.select_for_update().get(pk=row.pk)
    if not has_role(user, row.assigned_unit, MANAGERS):
        raise PermissionDenied
    before = {"unit": row.assigned_unit_id, "user": row.assigned_to_id}
    row.assigned_unit, row.assigned_to, row.notes = unit, assignee, reason.strip()
    row.save()
    AuditEvent.record("reconciliation.reassigned", obj=row, actor=user, org_unit_id=unit.pk,
                      data={"before": before, "unit": unit.pk, "user": assignee.pk if assignee else None, "reason": reason})
    return recheck(row, user)


@transaction.atomic
def return_for_correction(row, user, reason):
    if not row.invoice_id or not has_role(user, row.invoice.org_unit, MANAGERS) or not reason.strip():
        raise PermissionDenied("A scoped finance officer or unit administrator must give a correction reason.")
    invoice = Invoice.objects.select_for_update().get(pk=row.invoice_id)
    lock_provider(invoice.provider_id)
    row = StatementRow.objects.select_for_update().select_related("statement").get(pk=row.pk)
    if row.status != StatementRow.Status.UNSORTED or not row.statement.is_active or report(invoice)["valid"]:
        raise ValidationError("Only a current unresolved reconciliation exception can return an invoice.")
    if invoice.status not in [Invoice.Status.APPROVED, Invoice.Status.IN_APPROVAL, Invoice.Status.DISPUTED] or \
            invoice.settlement_items.exclude(line__instruction__status="cancelled").exists():
        raise ValidationError("Only invoices in approval, approved or disputed without an active payment instruction can be returned. Cancel the instruction first where applicable.")
    invoice.status, invoice.approved_at = Invoice.Status.RETURNED, None
    invoice.save(update_fields=["status", "approved_at", "updated_at"])
    invoice.approval_tasks.filter(status__in=["waiting", "pending"]).update(status="cancelled")
    AuditEvent.record("invoice.reconciliation_returned", obj=invoice, actor=user, org_unit_id=invoice.org_unit_id,
                      data={"reason": reason, "statement_row": row.pk})
    return invoice


def ensure_missing_line(invoice, statement):
    if statement.rows.filter(invoice_number__iexact=invoice.invoice_number).exists():
        return
    number = (statement.rows.aggregate(n=Max("line_number"))["n"] or 1) + 1
    StatementRow.objects.create(statement=statement, line_number=number, invoice=invoice,
        account_number=invoice.service_account.account_number, invoice_number=invoice.invoice_number,
        is_ledger_only=True, assigned_unit=invoice.org_unit,
        raw={"origin": "ledger_only", "reference": invoice.reference},
        parse_error="Recorded pending invoice is absent from the provider statement. Obtain a corrected listing or investigate its settlement status.",
        reason="Recorded pending invoice is absent from the provider statement. It is not reconciled.")


def invoice_saved(invoice):
    lock_provider(invoice.provider_id)
    invoice.reconciliation_rows.filter(status=StatementRow.Status.MATCHED).exclude(
        fingerprint=fingerprint(invoice)).update(status=StatementRow.Status.UNSORTED, fingerprint="",
                                                reason="Invoice details changed; recheck against the provider statement.")
    statement = latest_statement(invoice.org_unit, invoice.provider_id, invoice.currency)
    if statement:
        ensure_missing_line(invoice, statement)


def check_instruction(instruction, released=False):
    from operations.services import payable, require_clear
    """Fresh evidence before bank release; pinned evidence after sending records real execution."""
    items = list(instruction.lines.values_list("items__invoice_id", flat=True))
    invoices = list(Invoice.objects.filter(pk__in=items).select_related("provider", "org_unit", "service_account").order_by("pk"))
    if not invoices or len(invoices) != len(items):
        raise ValidationError("The instruction contains no invoices or inconsistent invoice links.")
    for line in instruction.lines.all():
        total = sum((payable(inv) for inv in invoices if inv.provider_id == line.provider_id), Decimal("0"))
        if total != line.amount:
            raise ValidationError("Instruction amounts differ from its recorded invoices. Cancel and rebuild the instruction.")
        for item in line.items.select_related("invoice"):
            if item.amount != payable(item.invoice) or item.invoice.currency != instruction.currency:
                raise ValidationError("An instruction item no longer matches its invoice amount or currency.")
    if sum((payable(invoice) for invoice in invoices), Decimal("0")) != instruction.total_amount:
        raise ValidationError("The instruction total no longer matches its invoices.")
    for pk in sorted({invoice.provider_id for invoice in invoices}):
        lock_provider(pk)
    for invoice in invoices:
        if not released:
            require_clear(invoice)
        baseline = PaymentEvidence.objects.filter(instruction=instruction, invoice=invoice).first()
        digest = fingerprint(invoice)
        if baseline and baseline.fingerprint != digest:
            raise ValidationError("An instruction invoice changed after preparation. Cancel and rebuild the instruction.")
        if released:
            if not baseline or not baseline.released_at:
                raise ValidationError("No reconciled bank-release evidence exists. Finance must investigate before recording execution.")
        else:
            row = require_reconciled(invoice)
            PaymentEvidence.objects.update_or_create(instruction=instruction, invoice=invoice,
                                                      defaults={"row": row, "fingerprint": digest})
