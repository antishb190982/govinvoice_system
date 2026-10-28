import io
from datetime import timedelta
from xml.etree import ElementTree as ET

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from imports import services as imports
from imports.models import ImportRow
from invoices.models import Invoice
from settlements import services as settle
from settlements.models import SettlementInstruction
from signatures.services import validate_pdf
from tests.conftest import PIN, pdf_bytes
from workflow import engine
from workflow.models import ApprovalTask


def approve(inv, p):
    engine.submit(inv, p["clerk"])
    engine.decide(inv.approval_tasks.get(status="pending"), p["verifier"], engine.Decision.APPROVE)
    engine.decide(inv.approval_tasks.get(status="pending"), p["approver"], engine.Decision.APPROVE, pin=PIN)
    inv.refresh_from_db()
    return inv


@pytest.fixture
def approved(signers, policy, make_invoice):
    return [approve(make_invoice(amt, due_in=d), signers) for amt, d in (("1000.000", 20), ("250.500", 12))]


def test_settlement_end_to_end(signers, tree, approved):
    p = signers
    si = settle.create_instruction(p["finance"], tree["moh"], "OMR", timezone.localdate() + timedelta(days=30),
                                   [i.pk for i in approved])
    assert si.total_amount == sum(i.amount_total for i in approved)
    assert si.lines.count() == 1 and si.pay_by == min(i.due_date for i in approved)
    assert Invoice.objects.filter(status=Invoice.Status.SCHEDULED).count() == 2

    settle.submit_for_authorisation(si, p["finance"], PIN)
    with pytest.raises(PermissionDenied):
        settle.authorise(si, p["finance"], PIN)  # preparer cannot authorise
    si = settle.authorise(si, p["authoriser"], PIN)

    si.pdf.open("rb")
    reports = validate_pdf(si.pdf.read())
    si.pdf.close()
    assert [r["field"] for r in reports] == ["Prepared", "Authorised"]
    assert all(r["ok"] and r["intact"] and r["trusted"] for r in reports), reports

    xml = settle.pain001(si)
    ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
    root = ET.fromstring(xml)
    assert root.find(".//p:GrpHdr/p:CtrlSum", ns).text == "1250.500"
    assert root.find(".//p:CdtTrfTxInf/p:CdtrAcct/p:Id/p:IBAN", ns).text == "OM810180000001299123456"

    settle.mark_executed(si, p["finance"], "BANK-REF-1", timezone.localdate())
    assert Invoice.objects.filter(status=Invoice.Status.PAID, payment_reference="BANK-REF-1").count() == 2


def test_tampered_pdf_fails_validation(signers, tree, approved):
    p = signers
    si = settle.create_instruction(p["finance"], tree["moh"], "OMR", timezone.localdate() + timedelta(days=30),
                                   [i.pk for i in approved])
    settle.submit_for_authorisation(si, p["finance"], PIN)
    si.refresh_from_db()
    si.pdf.open("rb")
    data = bytearray(si.pdf.read())
    si.pdf.close()
    idx = data.find(b"OMR 1,000.000")
    if idx < 0:
        idx = len(data) // 3
    data[idx] = (data[idx] + 1) % 256
    reports = validate_pdf(bytes(data))
    assert not all(r.get("ok") for r in reports)


def test_stored_pdf_hash_checked_before_countersigning(signers, tree, approved):
    p = signers
    si = settle.create_instruction(p["finance"], tree["moh"], "OMR", timezone.localdate() + timedelta(days=30),
                                   [i.pk for i in approved])
    settle.submit_for_authorisation(si, p["finance"], PIN)
    SettlementInstruction.objects.filter(pk=si.pk).update(pdf_sha256="0" * 64)
    with pytest.raises(ValidationError):
        settle.authorise(si, p["authoriser"], PIN)


def test_cancel_releases_invoices(signers, tree, approved):
    p = signers
    si = settle.create_instruction(p["finance"], tree["moh"], "OMR", timezone.localdate() + timedelta(days=30),
                                   [approved[0].pk])
    settle.cancel(si, p["finance"], "test")
    approved[0].refresh_from_db()
    assert approved[0].status == Invoice.Status.APPROVED


def test_only_finance_in_scope_can_prepare(signers, tree, approved):
    with pytest.raises(PermissionDenied):
        settle.create_instruction(signers["outsider"], tree["moh"], "OMR", timezone.localdate() + timedelta(days=30),
                                  [approved[0].pk])


# --- Imports ----------------------------------------------------------------------

CSV = (
    "Account No,Bill No,Bill Date,Due Date,Net,VAT,Total Due,Units\n"
    "ACC-1,B-100,{issue},{due},100.000,5.000,105.000,1200\n"
    "ACC-1,B-100,{issue},{due},100.000,5.000,105.000,1200\n"
    "UNKNOWN,B-101,{issue},{due},10,0,10,1\n"
    "ACC-1,B-102,{issue},{due},50,2.5,52.5,600\n"
)


def test_csv_import_stages_validates_and_commits(people, account):
    today = timezone.localdate()
    body = CSV.format(issue=(today - timedelta(days=3)).strftime("%d/%m/%Y"),
                      due=(today + timedelta(days=30)).strftime("%d/%m/%Y"))
    batch = imports.stage_spreadsheet(people["clerk"], SimpleUploadedFile("statement.csv", body.encode()),
                                      account.provider)
    rows = {r.row_number: r for r in batch.rows.all()}
    assert rows[2].status == ImportRow.Status.VALID
    assert any("duplicate of row 2" in e for e in rows[3].errors)
    assert any("not registered" in e for e in rows[4].errors)
    created, failed = imports.commit(batch, people["clerk"])
    assert len(created) == 2 and failed == 0
    inv = Invoice.objects.get(invoice_number="B-102")
    assert inv.status == Invoice.Status.DRAFT and inv.consumption == 600 and inv.document_sha256 == batch.sha256


def test_import_respects_unit_permissions(people, account):
    today = timezone.localdate()
    body = CSV.format(issue=today.isoformat(), due=(today + timedelta(days=30)).isoformat())
    batch = imports.stage_spreadsheet(people["outsider"], SimpleUploadedFile("s.csv", body.encode()), account.provider)
    assert not batch.rows.filter(status=ImportRow.Status.VALID).exists()


def test_xlsx_import(people, account):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Utility statement October"])
    ws.append([])
    ws.append(["Contract No", "Invoice Number", "Invoice Date", "Payment Due", "Amount"])
    today = timezone.localdate()
    ws.append(["ACC-1", "X-1", today - timedelta(days=2), today + timedelta(days=40), 321.5])
    buf = io.BytesIO()
    wb.save(buf)
    batch = imports.stage_spreadsheet(people["clerk"], SimpleUploadedFile("s.xlsx", buf.getvalue()), account.provider)
    row = batch.rows.get()
    assert row.status == ImportRow.Status.VALID, row.errors
    assert row.data["amount_total"] == "321.500"


def test_pdf_extraction(people, account):
    today = timezone.localdate()
    lines = ["Power Co", "TAX INVOICE", "Invoice No: PWR-7788", "Account No: ACC-1",
             f"Invoice Date: {(today - timedelta(days=1)):%d/%m/%Y}", "Consumption: 1,234 kWh",
             "VAT (5%): 12.000", "Total Amount Due: OMR 252.000", f"Due Date: {(today + timedelta(days=30)):%d/%m/%Y}"]
    f = SimpleUploadedFile("bill.pdf", pdf_bytes(lines))
    batch = imports.stage_pdfs(people["clerk"], [f], account.provider)
    row = batch.rows.get()
    assert row.status == ImportRow.Status.VALID, row.errors
    assert row.data["invoice_number"] == "PWR-7788" and row.data["amount_total"] == "252.000"
    imports.commit(batch, people["clerk"])
    inv = Invoice.objects.get(invoice_number="PWR-7788")
    assert inv.source == Invoice.Source.PDF and inv.document


def test_only_uploader_can_commit(people, account):
    today = timezone.localdate()
    body = CSV.format(issue=today.isoformat(), due=(today + timedelta(days=30)).isoformat())
    batch = imports.stage_spreadsheet(people["clerk"], SimpleUploadedFile("s.csv", body.encode()), account.provider)
    with pytest.raises(PermissionDenied):
        imports.commit(batch, people["verifier"])


def test_scheduler_alerts_finance_for_unscheduled_due_invoices(signers, policy, make_invoice):
    inv = approve(make_invoice(due_in=3), signers)
    assert inv.status == Invoice.Status.APPROVED
    settle.sweep_due()
    assert signers["finance"].notifications.filter(title__contains=inv.reference).exists()
    assert ApprovalTask.objects.filter(invoice=inv, status="approved").count() == 2
