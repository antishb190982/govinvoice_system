from decimal import Decimal
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from invoices import surcharges as fees
from invoices import services as invoice_services
from invoices.models import Flag
from operations import services as ops
from tests.test_operations import approved, authorise, login

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def valid_pin(monkeypatch):
    monkeypatch.setattr("signatures.services.check_pin", lambda user, pin: None)


def test_recorded_surcharge_is_inside_total_and_requires_review(make_invoice, people):
    invoice = make_invoice()
    result = fees.record(invoice, people["clerk"], Decimal("100"), "Late payment charge")
    assert result.amount_total == Decimal("1000") and ops.payable(result) == Decimal("1000")
    with pytest.raises(ValidationError, match="finance review"):
        ops.require_clear(result)
    fees.review(result, people["finance"], "Original charge checked")
    ops.require_clear(result)


@pytest.mark.parametrize("amount", ["-1", "1001"])
def test_invalid_charge_amount_rejected(make_invoice, people, amount):
    with pytest.raises(ValidationError):
        fees.record(make_invoice(), people["clerk"], Decimal(amount), "Invalid")


def test_scope_and_finance_only_assessment(make_invoice, people, tree):
    from tests.conftest import make_user
    from org.models import Role
    invoice = make_invoice()
    outsider = make_user("other-finance-fee", [(Role.FINANCE, tree["moe"])])
    with pytest.raises(PermissionDenied):
        fees.record(invoice, outsider, Decimal("100"), "Not own ministry")
    fees.record(invoice, people["clerk"], Decimal("100"), "Charge")
    with pytest.raises(PermissionDenied):
        fees.review(invoice, people["clerk"], "Not finance")
    with pytest.raises(ValidationError):
        fees.review(invoice, people["finance"], "")


def test_full_fee_waiver_uses_existing_authorisation_and_preserves_gross(make_invoice, people):
    invoice = fees.record(make_invoice(), people["clerk"], Decimal("100"), "Penalty")
    approved(invoice)
    item = ops.open_case(people["clerk"], invoice, "waiver", "Fee challenged", Decimal("100"), is_surcharge=True)
    authorise(people, item)
    invoice.refresh_from_db()
    assert invoice.amount_total == Decimal("1000") and invoice.surcharge_amount == Decimal("100")
    assert ops.payable(invoice) == Decimal("900") and invoice.status == "returned"
    assert fees.summary(invoice)["waived"] == Decimal("100")
    ops.require_clear(invoice)


def test_surcharge_case_cap_and_metadata_immutable_after_case(make_invoice, people):
    invoice = fees.record(make_invoice(), people["clerk"], Decimal("100"), "Penalty")
    with pytest.raises(ValidationError):
        ops.open_case(people["clerk"], invoice, "waiver", "Too much", Decimal("101"), is_surcharge=True)
    ops.open_case(people["clerk"], invoice, "issue", "Investigate", Decimal("0"), is_surcharge=True)
    with pytest.raises(ValidationError, match="cannot be rewritten"):
        fees.record(invoice, people["finance"], Decimal("50"), "Change after dispute")


def test_partial_fee_waiver_leaves_remainder_for_finance_review(make_invoice, people):
    invoice = fees.record(make_invoice(), people["clerk"], Decimal("100"), "Penalty")
    item = ops.open_case(people["clerk"], invoice, "waiver", "Partial correction", Decimal("40"), is_surcharge=True)
    authorise(people, item)
    with pytest.raises(ValidationError, match="finance review"):
        ops.require_clear(invoice)
    fees.review(invoice, people["finance"], "Remaining 60 is justified")
    ops.require_clear(invoice)
    assert fees.summary(invoice)["net_cost"] == Decimal("60")


def test_late_capture_and_refund_preserve_original_paid_evidence(make_invoice, people):
    invoice = make_invoice()
    invoice.status, invoice.payment_reference = "paid", "BANK-ORIGINAL"
    invoice.save()
    invoice.refresh_from_db()
    snapshot = invoice.snapshot()
    invoice = fees.record(invoice, people["finance"], Decimal("100"), "Late-classified fee")
    assert invoice.snapshot() == snapshot and invoice.payment_reference == "BANK-ORIGINAL"
    item = ops.open_case(people["clerk"], invoice, "refund", "Return fee", Decimal("100"), is_surcharge=True)
    item = authorise(people, item)
    ops.record_refund(item, people["finance"], "SUR-REF-PART", timezone.localdate(),
                      ContentFile(b"%PDF-proof", name="refund.pdf"), Decimal("40"))
    values = fees.summary(invoice)
    assert values["received"] == Decimal("40") and values["refund_due"] == Decimal("60")
    assert values["net_cost"] == Decimal("60") and values["waived"] == 0
    invoice.refresh_from_db()
    assert invoice.snapshot() == snapshot and invoice.status == "paid"


def test_changed_classification_clears_review(make_invoice, people):
    invoice = fees.record(make_invoice(), people["clerk"], Decimal("100"), "Fee")
    fees.review(invoice, people["finance"], "Checked")
    invoice = fees.record(invoice, people["finance"], Decimal("90"), "Corrected classification")
    assert not invoice.surcharge_reviewed_at


def test_net_may_include_or_exclude_surcharge_without_double_counting(make_invoice, people):
    invoice = make_invoice()
    invoice.surcharge_amount, invoice.amount_net, invoice.amount_tax = Decimal("100"), Decimal("900"), Decimal("0")
    invoice_services.save_invoice(invoice, people["clerk"])
    assert Flag.TOTAL_MISMATCH not in invoice.flags and invoice.amount_total == Decimal("1000")
    invoice.amount_net = Decimal("1000")
    invoice_services.save_invoice(invoice, people["clerk"])
    assert Flag.TOTAL_MISMATCH not in invoice.flags and invoice.amount_total == Decimal("1000")


def test_monitor_views_and_no_duplicate_totals_for_multiple_live_cases(client, make_invoice, people):
    invoice = fees.record(make_invoice(), people["clerk"], Decimal("100"), "Penalty")
    for reason in ["Check timing", "Check charge basis"]:
        ops.open_case(people["clerk"], invoice, "issue", reason, Decimal("0"), is_surcharge=True)
    login(client, people["finance"])
    result = client.get(reverse("invoices:surcharges"), {"status": "disputed"})
    assert result.status_code == 200 and result.context["charged"] == Decimal("100")
    assert result.context["providers"][0]["charged"] == Decimal("100")
    assert client.get(reverse("invoices:surcharge", args=[invoice.pk])).status_code == 200
    assert client.get(reverse("operations:case_new"), {"invoice": invoice.pk, "surcharge": "1"}).status_code == 200


def test_mixed_currencies_are_not_summed(client, make_invoice, people):
    mur = fees.record(make_invoice(), people["clerk"], Decimal("100"), "MUR fee")
    usd = make_invoice(currency="USD")
    fees.record(usd, people["clerk"], Decimal("20"), "USD fee")
    login(client, people["finance"])
    result = client.get(reverse("invoices:surcharges"))
    assert result.context["mixed_currencies"] and result.context["charged"] is None
    filtered = client.get(reverse("invoices:surcharges"), {"currency": "MUR"})
    assert not filtered.context["mixed_currencies"] and filtered.context["charged"] == Decimal("100")
