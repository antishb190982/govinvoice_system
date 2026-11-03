import csv
import io
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from pypdf import PdfReader

from accounts.middleware import OTP_SESSION_KEY
from accounts.models import User
from core.models import AuditEvent
from core.walkthrough_examples import AS_OF, scenarios
from integrations.models import ConfigEntry
from invoices.models import Invoice
from settlements.models import SettlementInstruction
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


def admin_client():
    user = make_user("walkthrough-admin", superuser=True)
    client = Client()
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()
    return client


def url(name, *args):
    return reverse("console:walkthrough" + name, args=args)


def test_catalogue_has_balanced_invoices_and_distinct_lifecycle_examples():
    values = scenarios()
    assert len(values) == 12
    assert sum(len(item["invoices"]) for item in values) == 36
    assert len({item["reference"] for item in values}) == 12
    assert {"approval_pending", "completed", "bank_mismatch", "bank_unmatched",
            "authorisation_pending", "disputed", "preparation"} <= {item["stage"] for item in values}
    for item in values:
        assert sum((inv["total"] for inv in item["invoices"]), Decimal("0")) == item["total"]
        assert len(item["journey"]) == 7
        for step in item["journey"]:
            assert step["when"] is None or step["when"] <= AS_OF
        for invoice in item["invoices"]:
            assert invoice["net"] == sum(row["amount"] for row in invoice["lines"])
            assert invoice["net"] + invoice["tax"] + invoice["surcharge"] == invoice["total"]
            assert invoice["received_date"] >= invoice["issue_date"]
            assert invoice["due_date"] >= invoice["issue_date"]


def test_group_retains_two_batches_and_bank_sum():
    items = scenarios()
    left, right = items[0], items[8]
    assert left["provider"] == right["provider"]
    assert left["ministry"] == right["ministry"]
    assert left["settlement"]["reference"] == right["settlement"]["reference"]
    expected = left["total"] + right["total"]
    assert left["settlement"]["total"] == expected
    assert left["bank"]["actual"] == left["bank"]["expected"] == expected
    assert {v["reference"] for v in left["settlement"]["batches"]} == {
        left["reference"], right["reference"],
    }


def test_unmatched_money_does_not_imply_approval_or_paid_invoices():
    item = next(v for v in scenarios() if v["stage"] == "bank_unmatched")
    assert item["settlement"] is None
    assert item["bank"]["expected"] is None
    assert item["bank"]["variance"] is None
    assert not item["evidence"]
    assert all(step["state"] != "done" for step in item["journey"])


def test_get_pages_filters_and_exports_never_write_financial_or_identity_data():
    client = admin_client()
    before = (
        User.objects.count(), Invoice.objects.count(), SettlementInstruction.objects.count(),
        ConfigEntry.objects.count(), AuditEvent.objects.count(),
    )
    response = client.get(url(""))
    assert response.status_code == 200
    assert response.context["summary"]["batches"] == 12
    assert b"Simulation only" in response.content
    filtered = client.get(url(""), {"provider": "CEB", "stage": "completed"})
    assert filtered.context["summary"]["batches"] == 2
    assert filtered.context["summary"]["invoices"] == 6
    assert len(filtered.context["scenarios"]) == 2
    empty = client.get(url(""), {"q": "not-a-real-scenario"})
    assert empty.context["summary"]["batches"] == 0
    searched_invoice = client.get(url(""), {"q": "SIM-INV-001-01"})
    assert searched_invoice.context["summary"]["batches"] == 1
    assert client.get(url(""), {"provider": "invalid"}).status_code == 404
    for item in scenarios():
        response = client.get(url("_detail", item["reference"]))
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        assert b"Simulation only" in response.content
    for suffix in (
        url("_invoice", "SIM-BATCH-001", "SIM-INV-001-01"),
        url("_remittance", "SIM-BATCH-001"),
        url("_bank", "SIM-BATCH-001"),
    ):
        assert client.get(suffix).status_code == 200
    after = (
        User.objects.count(), Invoice.objects.count(), SettlementInstruction.objects.count(),
        ConfigEntry.objects.count(), AuditEvent.objects.count(),
    )
    assert after == before


def test_group_remittance_retains_every_invoice_and_reference():
    response = admin_client().get(url("_remittance", "SIM-BATCH-001"))
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode())))
    assert len(rows) == 6
    assert all(row["simulation_only"] == "True" for row in rows)
    assert {row["batch_reference"] for row in rows} == {"SIM-BATCH-001", "SIM-BATCH-009"}
    assert len({row["invoice_reference"] for row in rows}) == 6
    assert all(row["bank_reference"].startswith("SIM-") for row in rows)
    assert sum(Decimal(row["amount"]) for row in rows) == scenarios()[0]["settlement"]["total"]
    assert "SIMULATION-ONLY" in response["Content-Disposition"]


def test_bank_examples_and_pdf_do_not_claim_real_evidence():
    client = admin_client()
    mismatch = client.get(url("_bank", "SIM-BATCH-005"))
    rows = list(csv.DictReader(io.StringIO(mismatch.content.decode())))
    assert rows[0]["simulation_only"] == "True"
    item = scenarios()[4]
    assert Decimal(rows[0]["debit"]) - item["total"] == Decimal("150.00")
    unknown = client.get(url("_bank", "SIM-BATCH-010"))
    assert list(csv.DictReader(io.StringIO(unknown.content.decode())))[0]["settlement_reference"] == ""
    response = client.get(url("_invoice", "SIM-BATCH-001", "SIM-INV-001-01"))
    assert response.status_code == 200
    text = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
    assert "SIMULATION ONLY - DO NOT PAY" in text
    assert "Not digitally signed" in text
    assert "No real user approval" in text
    assert client.get(url("_invoice", "SIM-BATCH-001", "SIM-INV-002-01")).status_code == 404
    assert client.get(url("_bank", "SIM-BATCH-007")).status_code == 404
    assert client.get(url("_remittance", "SIM-BATCH-007")).status_code == 404
    assert client.get(url("_detail", "unknown")).status_code == 404


def test_all_routes_require_admin_and_reject_mutations():
    anonymous = Client()
    admin = admin_client()
    restricted = Client()
    restricted.force_login(make_user("walkthrough-clerk"))
    session = restricted.session
    session[OTP_SESSION_KEY] = True
    session.save()
    routes = [
        url(""), url("_detail", "SIM-BATCH-001"),
        url("_invoice", "SIM-BATCH-001", "SIM-INV-001-01"),
        url("_bank", "SIM-BATCH-001"), url("_remittance", "SIM-BATCH-001"),
    ]
    for route in routes:
        assert anonymous.get(route).status_code == 302
        assert restricted.get(route).status_code == 403
        assert admin.post(route, {"status": "paid"}).status_code == 405
