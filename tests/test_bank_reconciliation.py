from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import OTP_SESSION_KEY
from core.models import AuditEvent
from org.models import Role
from providers.models import Provider
from reconciliation import bank
from reconciliation.models import BankAllocation, BankRow, BankStatement, BankTransactionKey
from settlements.models import SettlementInstruction, SettlementLine
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def executed(people, tree, provider):
    today = timezone.localdate()
    item = SettlementInstruction.objects.create(reference="SI-BANK-001", org_unit=tree["moh"],
        currency="MUR", cutoff_date=today, pay_by=today, requested_execution_date=today, status="executed",
        executed_at=today, bank_reference="EXEC-001", total_amount=Decimal("1000"),
        prepared_by=people["finance"], payer_name="Test treasury")
    SettlementLine.objects.create(instruction=item, provider=provider, amount=1000, invoice_count=1,
        earliest_due=today, beneficiary_name=provider.name, end_to_end_id="E2E-001")
    return item


def upload(people, tree, executed, **override):
    values = dict(transaction_id="TX-001", settlement_reference=executed.reference, provider_code="",
                  amount="1000", currency="MUR", value_date=timezone.localdate().isoformat(),
                  bank_reference="EXEC-001")
    values.update(override)
    csv = ",".join(bank.HEADERS) + "\n" + ",".join(values[h] for h in bank.HEADERS) + "\n"
    return bank.upload(people["finance"], tree["moh"], "MOH treasury MUR", "MUR",
                       timezone.localdate(), ContentFile(csv.encode(), name="bank.csv"))


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def test_exact_debit_links_executed_settlement_without_changing_payment(people, tree, executed):
    statement = upload(people, tree, executed)
    row = statement.rows.get()
    assert row.status == "matched"
    assert row.allocations.get().line.instruction == executed
    assert bank.bank_status(executed)["valid"]
    executed.refresh_from_db()
    assert executed.status == "executed" and executed.bank_reference == "EXEC-001"
    assert statement.document and len(statement.sha256) == 64
    assert AuditEvent.objects.filter(action="bank_reconciliation.checked").exists()


@pytest.mark.parametrize("fields,reason", [
    ({"amount": "999"}, "amount"), ({"currency": "USD"}, "currencies"),
    ({"settlement_reference": "UNKNOWN"}, "No unique"), ({"provider_code": "WRONG"}, "Provider code"),
    ({"bank_reference": "WRONG"}, "Bank reference"), ({"amount": "-1000"}, "non-negative"),
    ({"amount": "0"}, "positive"), ({"value_date": "29/10/2026"}, "YYYY-MM-DD"),
])
def test_mismatches_and_non_debits_are_exceptions(people, tree, executed, fields, reason):
    row = upload(people, tree, executed, **fields).rows.get()
    assert row.status == "exception" and reason.lower() in row.reason.lower()
    assert not BankAllocation.objects.exists() and not BankTransactionKey.objects.exists()


def test_sent_is_not_a_post_settlement_match(people, tree, executed):
    executed.status = "sent"
    executed.save()
    row = upload(people, tree, executed).rows.get()
    assert row.status == "exception" and "not recorded as executed" in row.reason
    executed.refresh_from_db()
    assert executed.status == "sent"


def test_duplicate_upload_does_not_allocate_again(people, tree, executed):
    first = upload(people, tree, executed).rows.get()
    second = upload(people, tree, executed).rows.get()
    assert second.status == "duplicate" and first.status == "matched"
    assert BankAllocation.objects.count() == BankTransactionKey.objects.count() == 1
    assert bank.bank_status(executed)["valid"]


def test_reused_debit_cannot_match_another_settlement(people, tree, executed):
    upload(people, tree, executed)
    executed.pk = None
    executed.reference = "SI-BANK-002"
    executed.save()
    old_line = SettlementLine.objects.first()
    old_line.pk = None
    old_line.instruction = executed
    old_line.save()
    row = upload(people, tree, executed).rows.get()
    assert row.status == "exception" and "Conflicting replay" in row.reason
    assert BankAllocation.objects.count() == 1


def test_duplicate_ids_inside_one_file_are_both_blocked(people, tree, executed):
    header = ",".join(bank.HEADERS) + "\n"
    line = f"TX-001,{executed.reference},,1000,MUR,{timezone.localdate()},EXEC-001\n"
    statement = bank.upload(people["finance"], tree["moh"], "MOH", "MUR", timezone.localdate(),
                            ContentFile((header + line + line).encode(), name="bank.csv"))
    assert statement.rows.filter(status="exception").count() == 2
    assert not BankAllocation.objects.exists()


def test_one_payment_cannot_be_allocated_to_different_transactions(people, tree, executed):
    upload(people, tree, executed)
    row = upload(people, tree, executed, transaction_id="TX-002").rows.get()
    assert row.status == "exception" and "already bank reconciled" in row.reason
    assert BankAllocation.objects.count() == 1


def test_provider_debits_support_partial_then_complete_batch(people, tree, executed):
    provider = Provider.objects.create(name="Water test", short_code="WTR", utility_type="water", currency="MUR")
    SettlementLine.objects.create(instruction=executed, provider=provider, amount=500, invoice_count=1,
        earliest_due=timezone.localdate(), beneficiary_name=provider.name, end_to_end_id="E2E-002")
    executed.total_amount = Decimal("1500")
    executed.save()
    upload(people, tree, executed, provider_code="PWR")
    assert bank.bank_status(executed)["label"] == "Partially reconciled"
    upload(people, tree, executed, transaction_id="TX-002", provider_code="WTR", amount="500")
    assert bank.bank_status(executed)["valid"]
    assert BankAllocation.objects.count() == 2


def test_timing_difference_needs_independent_review_not_an_amount_override(people, tree, executed):
    executed.executed_at = timezone.localdate() - timedelta(days=1)
    executed.save()
    row = upload(people, tree, executed).rows.get()
    assert row.status == "exception" and "independent" in row.reason
    row = bank.recheck(row, people["finance"], accept_date=True, reason="I uploaded this")
    assert row.status == "exception"
    row = bank.recheck(row, people["admin"], accept_date=True, reason="Bank posted next day")
    assert row.status == "matched" and row.reviewed_by == people["admin"]
    assert bank.recheck(row, people["finance"]).status == "matched"
    wrong = upload(people, tree, executed, transaction_id="TX-WRONG", amount="900").rows.get()
    assert bank.recheck(wrong, people["admin"], accept_date=True, reason="Cannot waive money").status == "exception"


def test_changed_execution_invalidates_evidence_and_releases_stale_allocation(people, tree, executed):
    row = upload(people, tree, executed).rows.get()
    executed.bank_reference = "CORRECTED-REF"
    executed.save()
    assert not bank.bank_status(executed)["valid"]
    row = bank.recheck(row, people["finance"])
    assert row.status == "exception" and "Bank reference" in row.reason
    assert not BankAllocation.objects.exists()


def test_finance_scope_and_read_only_permissions(client, people, tree, executed):
    statement = upload(people, tree, executed)
    foreign_finance = make_user("foreign-finance", [(Role.FINANCE, tree["moe"])])
    with pytest.raises(PermissionDenied):
        bank.recheck(statement.rows.get(), foreign_finance)
    with pytest.raises(PermissionDenied):
        bank.recheck(statement.rows.get(), people["clerk"])
    login(client, foreign_finance)
    assert client.get(statement.get_absolute_url()).status_code == 404
    assert client.get(reverse("reconciliation:bank_download", args=[statement.pk])).status_code == 404
    login(client, people["authoriser"])
    assert client.get(statement.get_absolute_url()).status_code == 200
    assert client.post(statement.rows.get().get_absolute_url(), {}).status_code == 403


def test_bank_csv_validation_and_future_statement_date(people, tree, executed):
    with pytest.raises(ValidationError):
        bank.parse(b"account,amount\n1,100\n")
    with pytest.raises(ValidationError):
        bank.parse(b"x" * (bank.MAX_BYTES + 1))
    with pytest.raises(ValidationError):
        bank.upload(people["finance"], tree["moh"], "MOH", "MUR",
            timezone.localdate() + timedelta(days=1), ContentFile(b"x", name="bank.csv"))
    with pytest.raises(ValidationError):
        bank.upload(people["finance"], tree["moh"], "MOH", "MUR",
            timezone.localdate(), ContentFile(b"x", name="bank.xlsx"))


def test_scoped_bank_upload_views_and_csv_template(client, people, tree, executed):
    login(client, people["finance"])
    response = client.get(reverse("reconciliation:bank_upload"))
    assert response.status_code == 200
    assert f'value="{timezone.localdate()}"' in response.content.decode()
    assert client.get(reverse("reconciliation:bank_template")).status_code == 200
    content = ",".join(bank.HEADERS) + f"\nWEB-001,{executed.reference},,1000,MUR,{timezone.localdate()},EXEC-001\n"
    response = client.post(reverse("reconciliation:bank_upload"), {
        "org_unit": tree["moh"].pk, "account_label": "MOH", "currency": "MUR",
        "statement_date": timezone.localdate().isoformat(),
        "document": ContentFile(content.encode(), name="web-bank.csv")})
    assert response.status_code == 302
    statement = BankStatement.objects.get(filename="web-bank.csv")
    assert client.get(response.url).status_code == 200
    assert client.get(statement.rows.get().get_absolute_url()).status_code == 200
    index = client.get(reverse("reconciliation:bank_index") + "?status=all")
    assert index.status_code == 200 and b"WEB-001" in index.content
    mismatch = upload(people, tree, executed, transaction_id="WEB-BAD", amount="900").rows.get()
    checked = client.post(mismatch.get_absolute_url(), {}, follow=True)
    assert b"Still needs investigation" in checked.content
    mismatch.refresh_from_db()
    assert mismatch.status == "exception"
