import csv
import hashlib
import io
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from invoices.models import Invoice
from settlements import services
from settlements.models import SettlementExport
from settlements.remittance import (
    entry_reference, invoice_entries, record_provider_delivery, retained_bytes,
    retained_export, schedule_bytes,
)
from tests.test_settlement_groups import approved_batches, group, login  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def authorised(approved_batches, people, tree, provider):
    instruction = group(approved_batches, people, tree, provider)
    instruction.status = "authorised"
    instruction.save(update_fields=["status"])
    return instruction


def rows(payload):
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


def export_url(instruction, fmt="csv"):
    return reverse("settlements:export", args=[instruction.pk, fmt])


def test_csv_has_individual_entries_and_retained_totals(client, authorised, approved_batches, people):
    login(client, people["finance"])
    response = client.get(export_url(authorised))
    assert response.status_code == 200
    entries = rows(response.content)
    assert len(entries) == 2
    assert sum(Decimal(row["Amount"]) for row in entries) == authorised.total_amount
    assert len({row["Entry reference"] for row in entries}) == 2
    assert all(len(row["Entry reference"]) <= 35 for row in entries)
    assert {row["Batch reference"] for row in entries} == {b.reference for b in approved_batches[0]}
    assert {row["Provider invoice number"] for row in entries} == {i.invoice_number for i in approved_batches[1]}
    assert all(row["Service account"] and row["Settlement group"] for row in entries)
    assert client.get(export_url(authorised)).content == response.content
    assert SettlementExport.objects.count() == 1
    assert SettlementExport.objects.get().sha256 == hashlib.sha256(response.content).hexdigest()
    detail = client.get(authorised.get_absolute_url())
    assert b"Bank file layout pending" in detail.content
    assert b"Individual invoice entries" in detail.content
    assert b"Record delivery outside GovBills" not in detail.content


def test_xml_has_one_transaction_per_invoice_without_truncated_references(authorised):
    root = ET.fromstring(services.pain001(authorised))
    ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
    transfers = root.findall(".//p:CdtTrfTxInf", ns)
    assert len(transfers) == 2
    assert all(node.text == "2" for node in root.findall(".//p:NbOfTxs", ns))
    assert sum(Decimal(node.find(".//p:InstdAmt", ns).text) for node in transfers) == authorised.total_amount
    assert {node.find(".//p:EndToEndId", ns).text for node in transfers} == {
        entry_reference(item) for item in invoice_entries(authorised)}
    for item in invoice_entries(authorised):
        assert any(item.invoice.invoice_number in node.find(".//p:Ustrd", ns).text for node in transfers)


def test_provider_receives_identical_bytes_without_marking_invoices_paid(client, authorised, people):
    login(client, people["finance"])
    original = client.get(export_url(authorised)).content
    assert client.get(export_url(authorised, "provider")).status_code == 403
    services.mark_sent(authorised, people["finance"])
    authorised.refresh_from_db()
    assert client.get(export_url(authorised, "provider")).content == original
    delivery = record_provider_delivery(authorised, people["finance"], "billing@example.com", "MESSAGE-TEST-01")
    assert delivery.data["sha256"] == hashlib.sha256(original).hexdigest()
    assert delivery.data["advice"] == "submitted_to_bank_not_paid"
    assert delivery.actor == people["finance"]
    assert record_provider_delivery(authorised, people["finance"], "billing@example.com", "MESSAGE-TEST-01").pk == delivery.pk
    assert AuditEvent.objects.filter(action="settlement.provider_delivery_recorded").count() == 1
    assert not Invoice.objects.filter(status="paid").exists()
    bank_event = AuditEvent.objects.get(action="settlement.sent")
    assert bank_event.data["schedule_sha256"] == delivery.data["sha256"]


def test_retained_copy_does_not_change_after_execution_or_live_label_edits(client, authorised, people):
    login(client, people["finance"])
    original = client.get(export_url(authorised)).content
    services.mark_sent(authorised, people["finance"])
    authorised.refresh_from_db()
    services.mark_executed(authorised, people["finance"], "TEST-BANK-REFERENCE", timezone.localdate())
    authorised.refresh_from_db()
    provider = authorised.lines.first().provider
    provider.name = "Renamed after execution"
    provider.save(update_fields=["name"])
    assert client.get(export_url(authorised, "provider")).content == original
    event = record_provider_delivery(authorised, people["finance"], "billing@example.com", "MESSAGE-TEST-02")
    assert event.data["advice"] == "payment_executed"


def test_delivery_form_requires_attestation_and_preserves_errors(client, authorised, people):
    login(client, people["finance"])
    services.mark_sent(authorised, people["finance"])
    url = reverse("settlements:action", args=[authorised.pk, "provider_sent"])
    values = {"recipient": "billing@example.com", "delivery_reference": "TEST-PORTAL-RECEIPT"}
    response = client.post(url, values)
    assert response.status_code == 200
    assert b"This field is required" in response.content
    assert not AuditEvent.objects.filter(action="settlement.provider_delivery_recorded").exists()
    response = client.post(url, {**values, "confirmed": "on"}, follow=True)
    assert b"No email was sent by GovBills" in response.content
    assert b"TEST-PORTAL-RECEIPT" in response.content


def test_roles_scope_and_state_are_enforced(client, authorised, people):
    with pytest.raises(ValidationError, match="bank submission"):
        record_provider_delivery(authorised, people["finance"], "billing@example.com", "TEST")
    with pytest.raises(PermissionDenied):
        retained_export(authorised, people["clerk"])
    login(client, people["outsider"])
    assert client.get(export_url(authorised)).status_code == 404
    login(client, people["clerk"])
    assert client.get(export_url(authorised)).status_code == 403
    login(client, people["finance"])
    assert client.get(export_url(authorised, "unrecognised-bank-format")).status_code == 404
    authorised.status = "cancelled"
    authorised.save(update_fields=["status"])
    assert client.get(export_url(authorised)).status_code == 403


def test_tampering_blocks_download_delivery_and_bank_release(authorised, people):
    artifact = retained_export(authorised, people["finance"])
    with artifact.file.open("wb") as target:
        target.write(b"tampered")
    with pytest.raises(ValidationError, match="integrity"):
        retained_bytes(artifact)
    with pytest.raises(ValidationError, match="integrity"):
        services.mark_sent(authorised, people["finance"])
    authorised.refresh_from_db()
    assert authorised.status == "authorised"
    assert not AuditEvent.objects.filter(action="settlement.sent").exists()


def test_csv_formula_injection_is_neutralised_and_archive_is_not_editable(authorised, people):
    entries = invoice_entries(authorised)
    entries[0].invoice.invoice_number = "=1+1"
    assert rows(schedule_bytes(authorised, entries))[0]["Provider invoice number"] == "'=1+1"
    artifact = retained_export(authorised, people["finance"])
    with pytest.raises(ValidationError, match="cannot be edited"):
        artifact.save()
