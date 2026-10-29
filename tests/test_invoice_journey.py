import pytest
from django.utils import timezone
from django.urls import reverse
from accounts.middleware import OTP_SESSION_KEY
from invoices.models import Invoice
from invoices.evidence import approval_evidence
from workflow.process import invoice_process

pytestmark = pytest.mark.django_db


def test_invoice_process_page_uses_compact_journey(client, people, make_invoice):
    invoice = make_invoice()
    client.force_login(people["clerk"])
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()

    response = client.get(reverse("workflow:process", args=[invoice.pk]))
    assert response.status_code == 200
    assert b'class="journey-flow"' in response.content
    assert b"Invoice journey" in response.content
    assert b"Current step" in response.content
    assert b'class="bpm-scroll"' not in response.content
    copy_step = next(step for step in response.context["nodes"] if step.get("watermark_copy"))
    assert not copy_step["copy_ready"]
    assert any("Final approval" in missing for missing in copy_step["missing_fields"])


def test_approved_invoice_journey_links_the_watermarked_copy(make_invoice, people):
    invoice = make_invoice()
    invoice.status = Invoice.Status.APPROVED
    invoice.approved_at = timezone.now()
    invoice.save(update_fields=["status", "approved_at"])

    journey = invoice_process(invoice, people["clerk"])
    copy_step = next(step for step in journey["nodes"] if step.get("watermark_copy"))
    assert copy_step["copy_ready"]
    assert copy_step["url"] == reverse("invoices:approved_copy", args=[invoice.pk])
    assert copy_step["bank_reference"] == "Pending bank settlement"
    assert any("Bank settlement reference" in missing for missing in copy_step["missing_fields"])
    assert approval_evidence(invoice)
