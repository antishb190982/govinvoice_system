import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from accounts.middleware import OTP_SESSION_KEY
from core.models import AuditEvent
from invoices.models import Invoice
from invoices.services import save_invoice
from org.models import Role
from reconciliation.models import Statement, StatementRow, PaymentEvidence
from reconciliation.parser import parse_statement
from reconciliation.services import (assign, fingerprint, recheck, report, return_for_correction,
                                     upload_statement, check_instruction)
from settlements import services as settlement
from workflow import engine
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


def upload(user, invoice, lines=None, date=None):
    content = "account_number,invoice_number,amount,currency\n" + (
        lines if lines is not None else
        f"{invoice.service_account.account_number},{invoice.invoice_number},{invoice.amount_total},{invoice.currency}\n")
    return upload_statement(user, invoice.provider, invoice.org_unit, invoice.currency,
        date or timezone.localdate(), ContentFile(content.encode(), name="provider-pending.csv"))


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def approved(invoice):
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.APPROVED)
    invoice.refresh_from_db()


def test_unreconciled_invoice_cannot_enter_approval_or_settlement(make_invoice, people, tree, policy):
    invoice = make_invoice(reconciled=False)
    with pytest.raises(ValidationError, match="reconciliation"):
        engine.submit(invoice, people["clerk"])
    approved(invoice)
    assert not settlement.eligible_invoices(tree["moh"], "MUR", invoice.due_date).exists()
    with pytest.raises(ValidationError):
        settlement.create_instruction(people["finance"], tree["moh"], "MUR", invoice.due_date, [invoice.pk])
    assert not invoice.approval_tasks.exists()


def test_exact_match_unlocks_approval_then_finance(people, policy, make_invoice, tree):
    invoice = make_invoice(reconciled=False)
    statement = upload(people["finance"], invoice)
    assert statement.document and statement.sha256
    assert report(invoice)["valid"] and statement.rows.get().status == "matched"
    engine.submit(invoice, people["clerk"])
    approved(invoice)
    instruction = settlement.create_instruction(people["finance"], tree["moh"], "MUR", invoice.due_date, [invoice.pk])
    assert instruction.reconciliation_evidence.get().fingerprint == fingerprint(invoice)


@pytest.mark.parametrize("account_no,amount,reason", [
    ("WRONG", "1000", "account"), ("ACC-1", "999", "Amount mismatch"), ("ACC-1", "-1", "non-negative"),
])
def test_mismatches_are_unsorted_and_cannot_be_forced(make_invoice, people, account_no, amount, reason):
    invoice = make_invoice(reconciled=False)
    statement = upload(people["finance"], invoice, f"{account_no},{invoice.invoice_number},{amount},MUR\n")
    row = statement.rows.get()
    assert row.status == "unsorted" and reason.lower() in row.reason.lower()
    assert not report(invoice)["valid"]
    row = recheck(row, people["clerk"])
    assert row.status == "unsorted"


def test_duplicates_both_stay_unsorted(make_invoice, people):
    invoice = make_invoice(reconciled=False)
    line = f"ACC-1,{invoice.invoice_number},1000,MUR\n"
    statement = upload(people["finance"], invoice, line + line)
    assert statement.rows.filter(status="unsorted").count() == 2
    assert not report(invoice)["valid"]


def test_missing_ledger_invoice_gets_explicit_unsorted_exception(make_invoice, people):
    invoice = make_invoice(reconciled=False)
    statement = upload(people["finance"], invoice, "UNKNOWN,OTHER,500,MUR\n")
    gap = statement.rows.get(invoice=invoice)
    assert gap.is_ledger_only and gap.status == "unsorted" and gap.amount is None
    assert "absent" in gap.reason
    assert statement.rows.count() == 2


def test_invoice_edit_invalidates_match_and_routes_back_to_basket(make_invoice, people):
    invoice = make_invoice(reconciled=False)
    statement = upload(people["finance"], invoice)
    invoice.amount_total = Decimal("1200")
    save_invoice(invoice, people["clerk"])
    assert not report(invoice)["valid"]
    assert statement.rows.get().status == "unsorted"
    assert "Amount mismatch" in recheck(statement.rows.get(), people["finance"]).reason
    invoice.amount_total = Decimal("1000")
    save_invoice(invoice, people["clerk"])
    recheck(statement.rows.get(), people["finance"])
    assert report(invoice)["valid"]


def test_new_listing_omission_revokes_old_evidence(make_invoice, people, policy):
    invoice = make_invoice(reconciled=False)
    old = upload(people["finance"], invoice)
    engine.submit(invoice, people["clerk"])
    new = upload(people["finance"], invoice, "UNKNOWN,OTHER,500,MUR\n")
    old.refresh_from_db()
    assert not old.is_active and not report(invoice)["valid"]
    task = invoice.approval_tasks.get(status="pending")
    with pytest.raises(ValidationError, match="reconciliation"):
        engine.decide(task, people["verifier"], engine.Decision.APPROVE)
    engine.decide(task, people["verifier"], engine.Decision.RETURN, "Investigate provider listing")
    assert new.rows.get(invoice=invoice).is_ledger_only


def test_assignment_and_recheck_are_scoped(make_invoice, people, tree):
    invoice = make_invoice(reconciled=False)
    statement = upload(people["finance"], invoice, f"WRONG,{invoice.invoice_number},1000,MUR\n")
    row = statement.rows.get()
    with pytest.raises(PermissionDenied):
        assign(row, people["outsider"], tree["moe"], people["outsider"], "Wrong unit")
    with pytest.raises(PermissionDenied):
        recheck(row, people["outsider"])
    assign(row, people["admin"], tree["hosp"], people["clerk"], "Clerk to check original service account")
    row.refresh_from_db()
    assert row.assigned_to == people["clerk"] and row.status == "unsorted"
    assert AuditEvent.objects.filter(action="reconciliation.reassigned").exists()


def test_approved_mismatch_returns_for_correction_without_erasing_history(make_invoice, people, policy):
    invoice = make_invoice()
    tasks = engine.submit(invoice, people["clerk"])
    engine.decide(tasks[0], people["verifier"], engine.Decision.APPROVE)
    approved(invoice)
    statement = upload(people["finance"], invoice, f"ACC-1,{invoice.invoice_number},1200,MUR\n")
    return_for_correction(statement.rows.get(), people["finance"], "Provider amount differs; verify original")
    invoice.refresh_from_db()
    assert invoice.status == "returned" and invoice.approval_tasks.filter(status="approved").exists()
    assert not invoice.approval_tasks.filter(status="pending").exists()


def test_mixed_finance_selection_is_atomic_not_silently_partial(make_invoice, people, tree):
    good = make_invoice()
    bad = make_invoice(reconciled=False)
    approved(good)
    approved(bad)
    with pytest.raises(ValidationError, match="partial"):
        settlement.create_instruction(people["finance"], tree["moh"], "MUR", good.due_date, [good.pk, bad.pk])
    assert not PaymentEvidence.objects.exists()
    good.refresh_from_db()
    assert good.status == "approved"


def test_changed_invoice_or_latest_statement_blocks_prepared_instruction(make_invoice, people, tree):
    invoice = make_invoice()
    approved(invoice)
    instruction = settlement.create_instruction(people["finance"], tree["moh"], "MUR", invoice.due_date, [invoice.pk])
    upload(people["finance"], invoice, "OTHER,OTHER,1,MUR\n")
    with pytest.raises(ValidationError, match="reconciliation"):
        check_instruction(instruction)
    Invoice.objects.filter(pk=invoice.pk).update(amount_total=Decimal("1500"))
    with pytest.raises(ValidationError):
        check_instruction(instruction)


def test_pin_after_bank_send_allows_recording_execution_when_provider_clears_pending(
        make_invoice, people, tree):
    invoice = make_invoice()
    approved(invoice)
    instruction = settlement.create_instruction(people["finance"], tree["moh"], "MUR", invoice.due_date, [invoice.pk])
    instruction.status = "authorised"
    instruction.save()
    settlement.mark_sent(instruction, people["finance"])
    upload(people["finance"], invoice, "OTHER,OTHER,1,MUR\n")
    settlement.mark_executed(instruction, people["finance"], "BANK-TEST", timezone.localdate())
    invoice.refresh_from_db()
    assert invoice.status == "paid"
    assert instruction.reconciliation_evidence.get().released_at


def test_parser_rejects_missing_columns_and_preserves_excel_identifiers():
    with pytest.raises(ValidationError, match="Required columns"):
        parse_statement(b"account,amount\n00123,10\n", "bad.csv", "MUR")
    book = Workbook()
    book.active.append(["account_number", "invoice_number", "amount", "currency"])
    book.active.append(["00123", "000456", 123.5, "MUR"])
    buffer = io.BytesIO()
    book.save(buffer)
    rows = parse_statement(buffer.getvalue(), "listing.xlsx", "MUR")
    assert rows[0]["account_number"] == "00123" and rows[0]["invoice_number"] == "000456"
    assert rows[0]["amount"] == Decimal("123.5")


def test_upload_rejects_old_and_future_statement_dates_and_foreign_unit(
        client, make_invoice, people, tree):
    invoice = make_invoice()
    with pytest.raises(ValidationError, match="older"):
        upload(people["finance"], invoice, date=timezone.localdate() - timedelta(days=1))
    login(client, people["outsider"])
    assert client.get(reverse("reconciliation:upload")).status_code == 403
    statement = Statement.objects.first()
    assert client.get(statement.get_absolute_url()).status_code == 404
    assert client.get(reverse("reconciliation:download", args=[statement.pk])).status_code == 404


def test_ui_scoped_upload_basket_statement_and_row_pages(client, make_invoice, people):
    invoice = make_invoice(reconciled=False)
    login(client, people["finance"])
    upload_page = client.get(reverse("reconciliation:upload"))
    assert f'value="{timezone.localdate().isoformat()}"' in upload_page.content.decode()
    response = client.post(reverse("reconciliation:upload"), {
        "provider": invoice.provider_id, "org_unit": invoice.org_unit_id, "currency": "MUR",
        "statement_date": timezone.localdate().isoformat(),
        "document": ContentFile(f"account_number,invoice_number,amount\nACC-1,{invoice.invoice_number},1\n".encode(), name="statement.csv"),
    })
    assert response.status_code == 302
    statement = Statement.objects.get()
    row = statement.rows.get()
    for url in [reverse("reconciliation:index"), statement.get_absolute_url(), row.get_absolute_url()]:
        assert client.get(url).status_code == 200
    assert client.get(reverse("workflow:process", args=[invoice.pk])).context["current"]["label"] == "Provider statement reconciliation"
    login(client, people["outsider"])
    assert client.get(row.get_absolute_url()).status_code == 404
