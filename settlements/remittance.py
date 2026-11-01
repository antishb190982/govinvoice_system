"""Invoice-level reference schedules, retained attachments and external delivery evidence.

The bank-specific layout is intentionally not implemented until supplied.
These CSV bytes are a reference schedule, not a certified bank upload format.
"""

import hashlib

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.validators import validate_email
from django.db import transaction
from django.utils.http import int_to_base36

from core.models import AuditEvent
from core.utils import csv_response
from org.access import has_role
from org.models import Role
from settlements.models import SettlementExport, SettlementInstruction, SettlementItem


def entry_reference(item):
    # Two bigint identifiers fit within the usual 35-character bank reference.
    return f"GB-{int_to_base36(item.line.instruction_id)}-{int_to_base36(item.pk)}".upper()


def invoice_entries(instruction):
    return list(SettlementItem.objects.filter(line__instruction=instruction).select_related(
        "line__provider", "invoice__service_account", "batch",
    ).order_by("pk"))


def schedule_bytes(instruction, entries):
    from settlements.services import _amount

    return csv_response("invoice-entries.csv", [
        "Settlement reference", "Settlement group", "Batch reference", "Batch name",
        "Entry reference", "Utility provider", "Provider invoice number", "GovBills invoice reference",
        "Service account", "Invoice due date", "Currency", "Amount",
        "Beneficiary name", "Beneficiary IBAN", "Beneficiary BIC", "Requested execution date",
    ], (
        [instruction.reference, instruction.group_reference or "",
         item.batch.reference if item.batch else "", item.batch.name if item.batch else "",
         entry_reference(item), item.line.provider.name, item.invoice.invoice_number,
         item.invoice.reference, item.invoice.service_account.account_number,
         item.invoice.due_date, instruction.currency, _amount(item.amount, instruction.currency),
         item.line.beneficiary_name, item.line.beneficiary_iban, item.line.beneficiary_bic,
         instruction.requested_execution_date]
        for item in entries
    )).content


def _require_operator(instruction, user):
    if not has_role(user, instruction.org_unit, [Role.FINANCE, Role.AUTHORISER]):
        raise PermissionDenied


@transaction.atomic
def retained_export(instruction, user):
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    _require_operator(instruction, user)
    if instruction.status not in ("authorised", "sent", "executed"):
        raise ValidationError("Only authorised settlement instructions can be exported.")
    existing = instruction.exports.filter(format_key="invoice-schedule-v1").first()
    if existing:
        return existing
    from reconciliation.services import check_instruction

    check_instruction(instruction, released=bool(instruction.sent_at))
    entries = invoice_entries(instruction)
    payload = schedule_bytes(instruction, entries)
    artifact = SettlementExport(
        instruction=instruction, created_by=user, entry_count=len(entries),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    artifact.file.save(f"{instruction.reference}-invoice-entries.csv", ContentFile(payload), save=False)
    try:
        artifact.save()
        AuditEvent.record("settlement.invoice_file_prepared", obj=instruction, actor=user,
            org_unit_id=instruction.org_unit_id, data={
                "export_id": artifact.pk, "sha256": artifact.sha256, "entries": len(entries),
                "format": artifact.format_key, "bank_layout": "pending",
            })
    except Exception:
        artifact.file.delete(save=False)
        raise
    return artifact


def retained_bytes(artifact):
    with artifact.file.open("rb") as source:
        payload = source.read()
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise ValidationError("The retained settlement file failed its integrity check. Do not send it.")
    return payload


@transaction.atomic
def record_provider_delivery(instruction, user, recipient, delivery_reference):
    instruction = SettlementInstruction.objects.select_for_update().get(pk=instruction.pk)
    _require_operator(instruction, user)
    if instruction.status not in ("sent", "executed"):
        raise ValidationError("Record bank submission before recording provider delivery.")
    providers = list(instruction.lines.order_by().values_list("provider_id", flat=True).distinct())
    if len(providers) != 1:
        raise ValidationError("A provider copy must contain only that provider's invoices. Split legacy multi-provider instructions.")
    recipient, delivery_reference = recipient.strip(), delivery_reference.strip()
    validate_email(recipient)
    if len(recipient) > 254 or not delivery_reference or len(delivery_reference) > 120:
        raise ValidationError("Enter the provider email and a delivery reference of at most 120 characters.")
    artifact = retained_export(instruction, user)
    retained_bytes(artifact)
    data = {
        "export_id": artifact.pk, "sha256": artifact.sha256, "entries": artifact.entry_count,
        "provider_id": providers[0], "recipient": recipient,
        "delivery_reference": delivery_reference, "delivery_method": "recorded_external",
        "advice": "payment_executed" if instruction.status == "executed" else "submitted_to_bank_not_paid",
        "format": artifact.format_key,
    }
    prior = AuditEvent.objects.filter(
        object_type="settlements.settlementinstruction", object_id=str(instruction.pk),
        action="settlement.provider_delivery_recorded", data__export_id=artifact.pk,
        data__recipient=recipient, data__delivery_reference=delivery_reference,
    ).first()
    if prior:
        return prior
    return AuditEvent.record("settlement.provider_delivery_recorded", obj=instruction, actor=user,
                             org_unit_id=instruction.org_unit_id, data=data)
