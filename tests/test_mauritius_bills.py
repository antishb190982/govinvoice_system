"""Mauritius Telecom statements + summary CSV and CEB e-Bills (synthetic look-alikes of real layouts)."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from imports import parsers
from imports import services as imports
from imports.models import ImportRow
from invoices.models import Flag, Invoice
from ocr.samples import ceb_ebill_pdf, mt_statement_pdf, mt_summary_csv
from providers.models import Provider, ServiceAccount, UtilityType
from tests.conftest import PIN
from workflow import engine


@pytest.fixture
def mt(tree):
    p = Provider.objects.create(name="Mauritius Telecom", short_code="MT", utility_type=UtilityType.TELECOM,
                                currency="MUR", tax_number="VAT20070778", bank_iban="MU31MCBL0901000000440011000MUR",
                                bank_account_name="MT", invoice_number_pattern=r"^FINV\d{8}-\d{4}[A-Z]$")
    ServiceAccount.objects.create(provider=p, org_unit=tree["hosp"], account_number="5500123", meter_number="4650001")
    return p


@pytest.fixture
def ceb(tree):
    p = Provider.objects.create(name="Central Electricity Board", short_code="CEB", utility_type=UtilityType.ELECTRICITY,
                                currency="MUR", tax_number="VAT22000591")
    # Registered without the leading zeros printed on the bill.
    ServiceAccount.objects.create(provider=p, org_unit=tree["hosp"], account_number="118900001", meter_number="11600001")
    return p


def test_parse_billing_periods():
    assert parsers.parse_period("September 2026") == (date(2026, 9, 1), date(2026, 9, 30))
    assert parsers.parse_period("février 2026") == (date(2026, 2, 1), date(2026, 2, 28))
    assert parsers.parse_period("07/09/2026 - 06/10/2026") == (date(2026, 9, 7), date(2026, 10, 6))
    assert parsers.parse_period("22.08.26 to 22.09.26") == (date(2026, 8, 22), date(2026, 9, 22))


def test_mt_summary_csv_without_choosing_provider(people, mt):
    csv = mt_summary_csv([
        ["5500123", "MINISTRY OF TEST", "4650001", "FINV05123456-2609A", "04/10/2026", "September 2026",
         "03/11/2026", "1380.00", "180.00", "1380.00", "250.00", "1630.00", "Invoice"],
        # Identified by its line number only (account number unknown to us).
        ["9999999", "MINISTRY OF TEST", "4650001", "FINV05123457-2609A", "04/10/2026", "September 2026",
         "03/11/2026", "0.00", "0.00", "0.00", "0.00", "0.00", "Invoice"],
    ])
    batch = imports.stage_spreadsheet(people["clerk"], SimpleUploadedFile("summary.csv", csv))  # no provider picked
    rows = list(batch.rows.order_by("row_number"))
    assert all(r.status == ImportRow.Status.VALID for r in rows), [r.errors for r in rows]
    assert rows[0].data["provider_code"] == "MT"  # from the FINV bill-number format
    assert rows[0].data["period_start"] == "2026-09-01" and rows[0].data["period_end"] == "2026-09-30"
    assert "arrears" in rows[0].data["warning"] and "nothing to pay" in rows[1].data["warning"]
    imports.commit(batch, people["clerk"])
    inv = Invoice.objects.get(invoice_number="FINV05123456-2609A")
    assert inv.amount_total == Decimal("1380") and inv.amount_tax == Decimal("180") and inv.amount_net == Decimal("1200")
    assert inv.arrears == Decimal("250") and Flag.ARREARS in inv.flags
    zero = Invoice.objects.get(invoice_number="FINV05123457-2609A")
    assert zero.service_account.account_number == "5500123" and Flag.NOTHING_TO_PAY in zero.flags


def test_mt_statement_pdf(people, mt):
    pdf = mt_statement_pdf(arrears="250.00")
    batch = imports.stage_documents(people["clerk"], [SimpleUploadedFile("statement.pdf", pdf)])
    row = batch.rows.get()
    ex = row.ocr_document.extraction
    assert row.ocr_document.provider == mt and ex["provider"]["confidence"] >= 0.95
    assert ex["account_number"]["value"] == "5500123"  # the billing account, not the customer account
    assert ex["service_number"]["value"] == "4650001"
    assert ex["invoice_number"]["value"] == "FINV05123456-2609A"
    assert ex["issue_date"]["value"] == "2026-10-04" and ex["due_date"]["value"] == "2026-11-03"
    assert (ex["period_start"]["value"], ex["period_end"]["value"]) == ("2026-09-01", "2026-09-30")
    assert ex["amount_total"]["value"] == "1380.000"  # this bill, not "Total amount due" incl. arrears
    assert ex["amount_net"]["value"] == "1200.000" and ex["amount_tax"]["value"] == "180.000"
    assert ex["arrears"]["value"] == "250.000"
    assert row.status == ImportRow.Status.VALID and not row.needs_review, (row.errors, row.warnings)


def test_ceb_ebill_pdf(people, ceb):
    pdf = ceb_ebill_pdf(arrears="120.50")
    batch = imports.stage_documents(people["clerk"], [SimpleUploadedFile("ceb.pdf", pdf)])
    row = batch.rows.get()
    ex = row.ocr_document.extraction
    assert row.ocr_document.provider == ceb
    assert ex["invoice_number"]["value"] == "250012345678"
    assert ex["issue_date"]["value"] == "2026-10-12" and ex["due_date"]["value"] == "2026-11-11"
    assert (ex["period_start"]["value"], ex["period_end"]["value"]) == ("2026-08-22", "2026-09-22")
    assert ex["amount_total"]["value"] == "1146.000"  # (B), not the (A+B) total
    assert ex["arrears"]["value"] == "120.500"
    assert ex["consumption"]["value"] == "166.000"
    assert ex["current_reading"]["value"] == "22865.000" and ex["previous_reading"]["value"] == "22699.000"
    # 000118900001 on the bill matches the account registered as 118900001.
    assert row.status == ImportRow.Status.VALID, row.errors
    assert row.data["account_number"] == "118900001"
    imports.commit(batch, people["clerk"])
    inv = Invoice.objects.get(invoice_number="250012345678")
    assert inv.consumption == 166 and inv.arrears == Decimal("120.5") and inv.source == Invoice.Source.PDF


def test_unregistered_account_offers_registration(client, people, mt):
    from django.urls import reverse

    from tests.test_views import sign_in

    pdf = mt_statement_pdf(bill_acc="5599999", line="4659999", bill="FINV05999999-2609A")
    batch = imports.stage_documents(people["clerk"], [SimpleUploadedFile("statement.pdf", pdf)], provider=mt)
    row = batch.rows.get()
    assert row.status == ImportRow.Status.INVALID and row.data["unregistered_account"] == "5599999"
    sign_in(client, people["clerk"])
    page = client.get(reverse("imports:batch", args=[batch.pk])).content.decode()
    assert "Register account" in page and "account_number=5599999" in page
    ServiceAccount.objects.create(provider=mt, org_unit=row.batch.uploaded_by.memberships.first().org_unit,
                                  account_number="5599999")
    imports.recheck(batch, people["clerk"])
    row.refresh_from_db()
    assert row.status == ImportRow.Status.VALID


def test_zero_amount_bill_closes_without_payment(signers, policy, mt, tree):
    from invoices.services import save_invoice
    from tests.conftest import upload_pdf

    acct = mt.accounts.get()
    inv = Invoice(service_account=acct, invoice_number="FINV05123458-2609A", issue_date=date(2026, 10, 4),
                  due_date=date(2026, 11, 3), amount_total=Decimal("0"))
    f = upload_pdf("zero.pdf")
    inv.document.save(f.name, f, save=False)
    save_invoice(inv, signers["clerk"])
    assert Flag.NOTHING_TO_PAY in inv.flags
    engine.submit(inv, signers["clerk"])
    engine.decide(inv.approval_tasks.get(status="pending"), signers["verifier"], engine.Decision.APPROVE)
    engine.decide(inv.approval_tasks.get(status="pending"), signers["approver"], engine.Decision.APPROVE, pin=PIN)
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.PAID and inv.payment_reference.startswith("NIL")
    from settlements.services import eligible_invoices

    assert not eligible_invoices(tree["moh"], "MUR", date(2027, 1, 1)).filter(pk=inv.pk).exists()
