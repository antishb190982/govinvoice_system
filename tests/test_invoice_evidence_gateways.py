import hashlib
import io
from datetime import timedelta
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from core.models import AuditEvent
from integrations import config
from integrations.models import OutboundMessage
from invoices.evidence import approval_evidence, render_approved_copy
from invoices.models import Invoice
from org.models import Role
from tests.conftest import make_user, pdf_bytes
from tests.test_views import sign_in
from workflow.models import ApprovalTask

pytestmark = pytest.mark.django_db


def approve(invoice, people):
    invoice.status = Invoice.Status.APPROVED
    invoice.approved_at = timezone.now()
    invoice.submitted_at = invoice.approved_at - timedelta(hours=1)
    invoice.save()
    for order, role, key in [(1, Role.VERIFIER, "verifier"), (2, Role.APPROVER, "approver")]:
        ApprovalTask.objects.create(invoice=invoice, round=1, order=order, name=role.title(), role=role,
            escalation_role=role, sla_hours=8, status="approved", decided_by=people[key],
            decided_at=invoice.approved_at, requires_signature=False)
    return invoice


def test_approved_copy_contains_people_dates_source_and_pending_bank(make_invoice, people, client):
    inv = approve(make_invoice(), people)
    inv.refresh_from_db()
    original = inv.document.read()
    original_hash, original_snapshot = inv.document_sha256, inv.snapshot()
    sign_in(client, people["clerk"])
    response = client.get(reverse("invoices:approved_copy", args=[inv.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert "no-store" in response["Cache-Control"]
    pdf = PdfReader(io.BytesIO(response.content))
    assert len(pdf.pages) >= 2
    text = "\n".join(page.extract_text() for page in pdf.pages)
    for value in ["Invoice 1", "GOVBILLS", "Verifier", "Approver", "Pending bank settlement", "Batch number",
                  inv.reference, original_hash, "not a new digital signature"]:
        assert value in text
    inv.refresh_from_db()
    with inv.document.open("rb") as f:
        assert f.read() == original
    assert inv.document_sha256 == original_hash and inv.snapshot() == original_snapshot
    event = AuditEvent.objects.filter(action="invoice.approved_copy_downloaded").last()
    assert event.data["copy_sha256"] == hashlib.sha256(response.content).hexdigest()


def test_copy_is_scoped_and_unapproved_documents_are_not_stamped(make_invoice, people, client):
    inv = make_invoice()
    sign_in(client, people["clerk"])
    assert client.get(reverse("invoices:approved_copy", args=[inv.pk])).status_code == 404
    inv = approve(inv, people)
    client.logout()
    sign_in(client, people["outsider"])
    assert client.get(reverse("invoices:approved_copy", args=[inv.pk])).status_code == 404
    assert client.get(reverse("invoices:approval_record", args=[inv.pk])).status_code == 404


def test_only_current_approval_round_actors_are_reported(make_invoice, people):
    inv = approve(make_invoice(), people)
    ApprovalTask.objects.create(invoice=inv, round=0, order=1, name="Old obsolete review",
        role=Role.VERIFIER, escalation_role=Role.VERIFIER, sla_hours=8, status="approved",
        decided_by=people["outsider"], decided_at=timezone.now())
    e = approval_evidence(inv)
    assert len(e["decisions"]) == 2
    assert all(d["person"] != str(people["outsider"]) for d in e["decisions"])


def test_no_source_generates_honest_approval_record(make_invoice, people):
    inv = approve(make_invoice(), people)
    inv.document = ""
    inv.save()
    data = render_approved_copy(inv, approval_evidence(inv))
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
    assert "No original invoice document is attached" in text


def test_changed_source_is_rejected_without_rewriting_it(make_invoice, people, client):
    inv = approve(make_invoice(), people)
    with inv.document.open("wb") as f:
        f.write(pdf_bytes(["Changed source"]))
    with pytest.raises(ValidationError, match="SHA-256"):
        render_approved_copy(inv, approval_evidence(inv))
    sign_in(client, people["clerk"])
    r = client.get(reverse("invoices:approved_copy", args=[inv.pk]))
    assert r.status_code == 302 and "approval-record" in r.url
    assert not AuditEvent.objects.filter(action="invoice.approved_copy_downloaded").exists()


def test_image_invoice_approved_copy(make_invoice, people):
    from PIL import Image
    from django.core.files.base import ContentFile

    inv = approve(make_invoice(), people)
    data = io.BytesIO()
    Image.new("RGB", (300, 500), "white").save(data, format="PNG")
    inv.document.save("image.png", ContentFile(data.getvalue()))
    inv.document_sha256 = hashlib.sha256(data.getvalue()).hexdigest()
    inv.save()
    assert len(PdfReader(io.BytesIO(render_approved_copy(inv, approval_evidence(inv)))).pages) >= 2


def test_batch_and_executed_bank_details_exclude_canceled_instructions(make_invoice, people, tree):
    from operations.models import InvoiceBatch, BatchMember
    from settlements.models import SettlementInstruction, SettlementLine, SettlementItem

    inv = approve(make_invoice(), people)
    batch = InvoiceBatch.objects.create(reference="BATCH-EVIDENCE", name="Evidence batch",
        org_unit=inv.org_unit, ministry=tree["moh"], provider=inv.provider, currency=inv.currency,
        created_by=people["clerk"], started_at=timezone.now())
    BatchMember.objects.create(batch=batch, invoice=inv, original_amount=inv.amount_total)
    for reference, status, bank in [("SI-LIVE", "sent", "premature-ref"), ("SI-CANCELED", "cancelled", "bad-ref")]:
        instruction = SettlementInstruction.objects.create(
            reference=reference, org_unit=inv.org_unit, currency=inv.currency, batch=batch,
            cutoff_date=inv.due_date, pay_by=inv.due_date, requested_execution_date=inv.due_date,
            status=status, prepared_by=people["finance"], payer_name="Test treasury", bank_reference=bank)
        line = SettlementLine.objects.create(instruction=instruction, provider=inv.provider,
            amount=inv.amount_total, invoice_count=1, earliest_due=inv.due_date,
            beneficiary_name="Test provider", end_to_end_id=reference)
        SettlementItem.objects.create(line=line, invoice=inv, amount=inv.amount_total)
    e = approval_evidence(inv)
    assert e["batch_reference"] == "BATCH-EVIDENCE" and e["settlement_reference"] == "SI-LIVE"
    assert e["bank_reference"] == "Pending bank settlement" and e["settled_at"] is None
    SettlementInstruction.objects.filter(reference="SI-LIVE").update(
        status="executed", executed_at=timezone.localdate(), bank_reference="BANK-REC-567")
    e = approval_evidence(inv)
    assert e["bank_reference"] == "BANK-REC-567" and e["settled_at"] == timezone.localdate()
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(render_approved_copy(inv, e))).pages)
    assert "BANK-REC-567" in text and "BATCH-EVIDENCE" in text and "bad-ref" not in text


def test_rotated_multipage_source_keeps_all_pages(make_invoice, people):
    from pypdf import PdfWriter
    from django.core.files.base import ContentFile

    inv = approve(make_invoice(), people)
    writer = PdfWriter()
    for n in range(2):
        page = PdfReader(io.BytesIO(pdf_bytes([f"Source page {n}"]))).pages[0]
        page.rotate(90 if n else 0)
        writer.add_page(page)
    data = io.BytesIO()
    writer.write(data)
    inv.document.save("multipage.pdf", ContentFile(data.getvalue()))
    inv.document_sha256 = hashlib.sha256(data.getvalue()).hexdigest()
    inv.save()
    result = PdfReader(io.BytesIO(render_approved_copy(inv, approval_evidence(inv))))
    assert len(result.pages) >= 3
    for n in range(2):
        text = result.pages[n].extract_text()
        assert f"Source page {n}" in text and "Bank settlement reference" in text


def test_gateway_disabled_and_test_confirmation_does_not_send(client, people):
    root = make_user("rootgateway", superuser=True)
    sign_in(client, root)
    r = client.get(reverse("console:gateways"))
    assert r.status_code == 200 and b"Gateway centre" in r.content
    assert not r.context["channels"]["email"]["ready"]
    with mock.patch("integrations.mail.ConfigurableEmailBackend.send_messages") as send:
        r = client.post(reverse("console:email"), {"action": "test", "to": "test@example.gov"})
        assert r.status_code == 200 and r.context["test"].errors["confirm"]
        r = client.post(reverse("console:email"), {"action": "test", "to": "test@example.gov", "confirm": "on"})
        assert r.status_code == 302
        assert not send.called
    with mock.patch("integrations.sms.send_raw") as send:
        client.post(reverse("console:sms"), {"action": "test", "to": "57123456", "confirm": "on"})
        assert not send.called


def test_email_test_uses_own_transport_result_not_global_last_log(client, settings):
    root = make_user("gatewayroot", superuser=True)
    sign_in(client, root)
    settings.EMAIL_BACKEND = "integrations.mail.ConfigurableEmailBackend"
    settings.SMTP_ENABLED, settings.SMTP_HOST = True, "smtp.example.gov"
    OutboundMessage.objects.create(channel="email", recipient="other@example.gov", status="skipped")
    with mock.patch("integrations.mail.ConfigurableEmailBackend.send_messages", return_value=1):
        response = client.post(reverse("console:email"),
            {"action": "test", "to": "test@example.gov", "confirm": "on"}, follow=True)
    assert b"submitted to SMTP" in response.content
    with mock.patch("integrations.mail.ConfigurableEmailBackend.send_messages", side_effect=RuntimeError("private-token")):
        response = client.post(reverse("console:email"),
            {"action": "test", "to": "test@example.gov", "confirm": "on"}, follow=True)
    assert b"submission failed" in response.content and b"private-token" not in response.content


def test_gateway_validation_blocks_incomplete_enabled_channels(people):
    for values in [
        {"SMTP_ENABLED": True, "SMTP_HOST": "", "SMTP_FROM": ""},
        {"SMTP_ENABLED": True, "SMTP_HOST": "smtp.example.gov", "SMTP_FROM": "sender@example.gov", "SMTP_PORT": 0},
        {"SMS_ENABLED": True, "SMS_URL": ""},
        {"SMS_ENABLED": True, "SMS_URL": "http://sms.example.gov"},
    ]:
        with pytest.raises(ValidationError):
            config.save(values, people["admin"])
