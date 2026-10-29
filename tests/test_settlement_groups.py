from decimal import Decimal
import io

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from accounts.middleware import OTP_SESSION_KEY
from core.models import AuditEvent
from invoices.models import Invoice
from operations.models import InvoiceBatch
from operations.services import create_batch
from operations.views import events
from operations.workspace import batches_for
from org.models import Role
from providers.models import Provider, ServiceAccount
from reconciliation.services import check_instruction
from settlements.grouping import create_group, ready_batches
from settlements.models import SettlementBatch, SettlementInstruction
from settlements.services import cancel, create_instruction
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def approved_batches(make_invoice, people, tree):
    batches = []
    invoices = []
    for index, amount in enumerate(("125.000", "275.000")):
        invoice = make_invoice(amount=amount)
        batch = create_batch(people["clerk"], tree["moh"], f"Approved group batch {index}", [invoice.pk])
        invoice.status = Invoice.Status.APPROVED
        invoice.approved_at = timezone.now()
        invoice.save()
        batches.append(batch)
        invoices.append(invoice)
    return batches, invoices


def group(approved_batches, people, tree, provider, **kwargs):
    batches, _ = approved_batches
    return create_group(people["finance"], tree["moh"], provider, "MUR",
                        kwargs.pop("batch_ids", [b.pk for b in batches]), **kwargs)


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def test_group_reserves_whole_batches_snapshots_and_audit(approved_batches, people, tree, provider):
    si = group(approved_batches, people, tree, provider, notes="Monthly combined settlement")
    batches, invoices = approved_batches
    assert si.group_reference.startswith("SG-") and si.batch_id is None
    assert si.total_amount == Decimal("400")
    assert list(si.batch_links.values_list("amount", flat=True)) == [Decimal("125"), Decimal("275")]
    assert set(si.lines.first().items.values_list("batch_id", flat=True)) == {b.pk for b in batches}
    assert Invoice.objects.filter(pk__in=[i.pk for i in invoices], status="scheduled").count() == 2
    for batch in batches:
        assert batch.lifecycle == "Settlement preparation"
        assert list(batch.payment_instructions) == [si]
        assert events(batch=batch).filter(action="settlement.group_created").exists()
        assert events(batch=batch).filter(action="batch.settlement_grouped", object_id=str(batch.pk)).exists()
    assert not ready_batches(people["finance"], tree["moh"], provider, "MUR")
    check_instruction(si)


def test_duplicate_selection_is_atomic(approved_batches, people, tree, provider):
    si = group(approved_batches, people, tree, provider)
    with pytest.raises(ValidationError):
        group(approved_batches, people, tree, provider)
    assert SettlementInstruction.objects.count() == 1
    assert si.batch_links.count() == 2


def test_cancel_preserves_old_group_and_allows_new_group(approved_batches, people, tree, provider):
    old = group(approved_batches, people, tree, provider)
    cancel(old, people["finance"], "Correct payment notes")
    assert old.batch_links.count() == 2
    assert len(ready_batches(people["finance"], tree["moh"], provider, "MUR")) == 2
    new = group(approved_batches, people, tree, provider)
    assert new.group_reference != old.group_reference
    for batch in approved_batches[0]:
        assert batch.payment_instructions.count() == 2
        assert events(batch=batch).filter(action="settlement.cancelled").exists()


@pytest.mark.parametrize("status", ["draft", "in_approval", "returned", "disputed", "paid", "cancelled"])
def test_unapproved_batch_rejected_atomically(status, approved_batches, people, tree, provider):
    invoice = approved_batches[1][1]
    invoice.status = status
    invoice.save()
    with pytest.raises(ValidationError):
        group(approved_batches, people, tree, provider)
    assert not SettlementInstruction.objects.exists()
    approved_batches[1][0].refresh_from_db()
    assert approved_batches[1][0].status == "approved"


def test_reconciliation_required_for_every_member(approved_batches, people, tree, provider):
    from reconciliation.models import StatementRow
    StatementRow.objects.filter(invoice=approved_batches[1][1]).update(status="mismatch")
    with pytest.raises(ValidationError):
        group(approved_batches, people, tree, provider)
    assert not SettlementBatch.objects.exists()


def test_held_account_not_groupable(approved_batches, people, tree, provider):
    approved_batches[1][0].service_account.__class__.objects.filter(
        pk=approved_batches[1][0].service_account_id).update(status="on_hold")
    with pytest.raises(ValidationError):
        group(approved_batches, people, tree, provider)
    assert not ready_batches(people["finance"], tree["moh"], provider, "MUR")


def test_partial_batch_selection_rejected(approved_batches, people, tree, provider):
    invoice = approved_batches[1][0]
    with pytest.raises(ValidationError, match="every approved payable"):
        create_instruction(people["finance"], tree["moh"], "MUR", invoice.due_date, [invoice.pk],
                           provider=provider, batch_ids=[b.pk for b in approved_batches[0]])
    assert not SettlementInstruction.objects.exists()


def test_mixed_provider_rejected(approved_batches, people, tree, provider):
    other = Provider.objects.create(name="Other provider", short_code="OTHER", utility_type="water")
    InvoiceBatch.objects.filter(pk=approved_batches[0][1].pk).update(provider=other)
    with pytest.raises(ValidationError, match="one utility provider"):
        group(approved_batches, people, tree, provider)
    assert not SettlementInstruction.objects.exists()


def test_mixed_currency_rejected(approved_batches, people, tree, provider):
    InvoiceBatch.objects.filter(pk=approved_batches[0][1].pk).update(currency="USD")
    with pytest.raises(ValidationError, match="one currency"):
        group(approved_batches, people, tree, provider)


def test_ministry_and_role_boundaries(approved_batches, people, tree, provider):
    outsider = make_user("other-finance", [(Role.FINANCE, tree["moe"])])
    with pytest.raises(PermissionDenied):
        create_group(outsider, tree["moh"], provider, "MUR", [b.pk for b in approved_batches[0]])
    with pytest.raises(PermissionDenied):
        create_group(people["clerk"], tree["moh"], provider, "MUR", [b.pk for b in approved_batches[0]])
    assert not SettlementInstruction.objects.exists()


def test_cross_ministry_group_rejected(approved_batches, people, tree, provider):
    batches, invoices = approved_batches
    foreign = ServiceAccount.objects.create(provider=provider, org_unit=tree["moe"], account_number="OTHER-MINISTRY")
    invoices[1].service_account = foreign
    invoices[1].org_unit = tree["moe"]
    invoices[1].save()
    InvoiceBatch.objects.filter(pk=batches[1].pk).update(org_unit=tree["moe"], ministry=tree["moe"])
    national_finance = make_user("national-finance", [(Role.FINANCE, tree["gov"])])
    with pytest.raises(ValidationError):
        create_group(national_finance, tree["gov"], provider, "MUR", [b.pk for b in batches])
    assert not SettlementInstruction.objects.exists()


def test_frozen_group_integrity_and_zero_bank_release(approved_batches, people, tree, provider):
    si = group(approved_batches, people, tree, provider)
    si.batch_links.filter(batch=approved_batches[0][0]).update(amount=Decimal("126"))
    with pytest.raises(ValidationError, match="batch amounts"):
        check_instruction(si)
    assert not si.reconciliation_evidence.filter(released_at__isnull=False).exists()


def test_group_pdf_and_invoice_evidence_preserve_batch(approved_batches, people, tree, provider):
    from invoices.evidence import approval_evidence, _record_rows
    from settlements.pdf import render
    si = group(approved_batches, people, tree, provider)
    text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(render(si))).pages)
    assert si.group_reference in text
    for batch, invoice in zip(*approved_batches):
        evidence = approval_evidence(invoice)
        assert evidence["batch_reference"] == batch.reference
        assert evidence["settlement_group"] == si.group_reference
        assert ("Settlement group", si.group_reference) in _record_rows(invoice, evidence)
        assert batch.reference in text


def test_group_frontend_creation_detail_audit_and_filters(client, approved_batches, people, tree, provider):
    login(client, people["finance"])
    url = reverse("settlements:group_new")
    query = f"?provider={provider.pk}&org_unit={tree['moh'].pk}&currency=MUR"
    response = client.get(url + query)
    assert response.status_code == 200
    for batch in approved_batches[0]:
        assert batch.reference.encode() in response.content
    response = client.post(url + query, {"batch": [b.pk for b in approved_batches[0]], "notes": "Combined"})
    assert response.status_code == 302
    si = SettlementInstruction.objects.get()
    response = client.get(si.get_absolute_url())
    assert response.status_code == 200 and si.group_reference.encode() in response.content
    assert b"Member batches" in response.content and b"settlement.group_created" in response.content
    for batch in approved_batches[0]:
        response = client.get(batch.get_absolute_url())
        assert response.status_code == 200 and si.group_reference.encode() in response.content
    response = client.get(reverse("settlements:list") + "?groups=1")
    assert si.group_reference.encode() in response.content
    assert b"Group approved batches" in response.content


def test_empty_and_invalid_group_post(client, approved_batches, people, tree, provider):
    login(client, people["finance"])
    url = reverse("settlements:group_new")
    query = f"?provider={provider.pk}&org_unit={tree['moh'].pk}&currency=MUR"
    for data in ({}, {"batch": ["invalid"]}, {"batch": ["999999"]}):
        response = client.post(url + query, data)
        assert response.status_code == 200
        assert not SettlementInstruction.objects.exists()


def test_foreign_ministry_cannot_view_group(client, approved_batches, people, tree, provider):
    si = group(approved_batches, people, tree, provider)
    login(client, people["outsider"])
    assert client.get(si.get_absolute_url()).status_code == 404
    assert client.get(reverse("settlements:group_new")).status_code == 403


def test_paid_and_returned_group_updates_batch_folders(approved_batches, people, tree, provider):
    si = group(approved_batches, people, tree, provider)
    si.rejection_comment = "Needs correction"
    si.save()
    ids = [b.pk for b in approved_batches[0]]
    assert set(batches_for(people["finance"]).filter(pk__in=ids).values_list("batch_folder", flat=True)) == {"rejected"}
    si.rejection_comment = ""
    si.status = "executed"
    si.save()
    assert set(batches_for(people["finance"]).filter(pk__in=ids).values_list("batch_folder", flat=True)) == {"completed"}


def test_group_sign_authorise_execute_and_bank_match(approved_batches, signers, tree, provider):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from reconciliation.bank import upload
    from settlements import services
    from signatures.services import validate_pdf
    from tests.conftest import PIN

    si = group(approved_batches, signers, tree, provider)
    si = services.submit_for_authorisation(si, signers["finance"], PIN)
    with pytest.raises(PermissionDenied):
        services.authorise(si, signers["finance"], PIN)
    si = services.authorise(si, signers["authoriser"], PIN)
    assert len(validate_pdf(si.pdf.read())) == 2
    services.mark_sent(si, signers["finance"])
    services.mark_executed(si, signers["finance"], "TEST-GROUP-NO-TRANSFER", timezone.localdate())
    si.refresh_from_db()
    for batch in approved_batches[0]:
        assert batch.lifecycle == "Settled"
        assert events(batch=batch).filter(action="settlement.executed").exists()
    assert si.batch_links.count() == 2
    data = (f"transaction_id,settlement_reference,amount,currency,value_date,provider_code,bank_reference\n"
            f"TEST-GROUP-DEBIT,{si.reference},400.000,MUR,{timezone.localdate()},"
            f"{provider.short_code},TEST-GROUP-NO-TRANSFER\n").encode()
    statement = upload(signers["finance"], tree["moh"], "Test bank", "MUR", timezone.localdate(),
                       SimpleUploadedFile("group-bank.csv", data, content_type="text/csv"))
    assert statement.rows.get().status == "matched"
