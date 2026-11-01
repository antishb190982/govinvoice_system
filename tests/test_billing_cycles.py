from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import OTP_SESSION_KEY
from imports.models import ImportRow
from imports.services import stage_spreadsheet, commit
from invoices.models import Invoice
from invoices.services import save_invoice
from providers.cycle_views import BillingCycleForm
from providers.cycles import classify
from providers.models import BillingCycle, ServiceAccount
from reconciliation.models import Statement, StatementRow
from reconciliation.services import upload_statement, report, recheck, assign_statement_cycle

pytestmark = pytest.mark.django_db


@pytest.fixture
def invoice_factory(make_invoice):
    return make_invoice


@pytest.fixture
def cycles(provider):
    today = timezone.localdate()
    return [BillingCycle.objects.create(provider=provider, code=code, name=f"Named cycle {code}",
            period_start=today - timedelta(days=30), period_end=today)
            for code in ("CYCLE-A", "CYCLE-B")]


def source(people, account, cycle, reference, amount="100.000", unit=None):
    content = f"account_number,invoice_number,amount,currency\n{account.account_number},{reference},{amount},MUR\n"
    return upload_statement(people["finance"], account.provider, unit or account.org_unit, "MUR",
                            timezone.localdate(), ContentFile(content.encode(), name="cycle.csv"),
                            billing_cycle=cycle)


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def test_overlapping_named_cycles_are_not_merged(cycles, provider):
    assert cycles[0].pk != cycles[1].pk
    assert classify(provider, cycles[0].period_start, cycles[0].period_end).is_unassigned
    assert classify(provider, selected=cycles[1]) == cycles[1]
    data = {"provider": provider.pk, "code": "cycle-c", "name": "Third cycle",
            "period_start": cycles[0].period_start, "period_end": cycles[0].period_end}
    form = BillingCycleForm(data)
    assert form.is_valid(), form.errors
    assert form.save().code == "CYCLE-C"


def test_auto_reconciles_invoice_arriving_after_file(cycles, people, account, invoice_factory, tree):
    # File covers a parent ministry, but the clerk only needs capture rights on the invoice unit.
    statement = source(people, account, cycles[0], "AFTER-FILE", unit=tree["moh"])
    row = statement.rows.get()
    assert row.status == StatementRow.Status.UNSORTED
    invoice = invoice_factory(number="AFTER-FILE", amount="100.000", reconciled=False,
                              period_start=cycles[0].period_start, period_end=cycles[0].period_end)
    invoice.refresh_from_db()
    row.refresh_from_db()
    assert invoice.billing_cycle == cycles[0]
    assert row.assigned_unit_id == invoice.org_unit_id
    assert row.status == StatementRow.Status.MATCHED
    assert report(invoice)["valid"]
    assert invoice.status == Invoice.Status.DRAFT
    assert not invoice.approval_tasks.exists()
    assert not invoice.settlement_items.exists()


@pytest.mark.parametrize("field,value", [
    ("amount", "101.000"), ("account", "WRONG-ACCOUNT"), ("currency", "USD"),
    ("duplicate", "duplicate"),
])
def test_auto_match_rejects_mismatches(cycles, people, account, invoice_factory, field, value):
    account_number = value if field == "account" else account.account_number
    amount = value if field == "amount" else "100.000"
    currency = value if field == "currency" else "MUR"
    line = f"{account_number},FAIL-AUTO,{amount},{currency}\n"
    content = "account_number,invoice_number,amount,currency\n" + line + (line if field == "duplicate" else "")
    statement = upload_statement(people["finance"], account.provider, account.org_unit, "MUR",
        timezone.localdate(), ContentFile(content.encode(), name="mismatch.csv"), billing_cycle=cycles[0])
    invoice = invoice_factory(number="FAIL-AUTO", amount="100.000", reconciled=False, billing_cycle=cycles[0])
    assert not report(invoice)["valid"]
    assert not statement.rows.filter(status=StatementRow.Status.MATCHED).exists()
    assert invoice.status == Invoice.Status.DRAFT


def test_files_remain_current_independently_per_cycle(cycles, people, account, invoice_factory):
    first = source(people, account, cycles[0], "A")
    second = source(people, account, cycles[1], "B")
    a = invoice_factory(number="A", amount="100", reconciled=False, billing_cycle=cycles[0])
    b = invoice_factory(number="B", amount="100", reconciled=False, billing_cycle=cycles[1])
    assert report(a)["valid"] and report(b)["valid"]
    replacement = source(people, account, cycles[0], "A")
    first.refresh_from_db()
    second.refresh_from_db()
    assert not first.is_active and second.is_active
    assert report(a)["statement"] == replacement
    assert report(b)["statement"] == second


def test_cycle_cannot_cross_provider_or_override_period(cycles, account, provider, invoice_factory):
    from providers.models import Provider
    other = Provider.objects.create(name="Other", short_code="OTHER", utility_type="water")
    with pytest.raises(ValidationError, match="different utility provider"):
        classify(other, selected=cycles[0])
    with pytest.raises(ValidationError, match="outside"):
        invoice_factory(number="BAD-PERIOD", billing_cycle=cycles[0],
                        period_start=cycles[0].period_start - timedelta(days=1),
                        period_end=cycles[0].period_end, reconciled=False)


def test_same_reference_in_multiple_cycles_is_not_guessed(cycles, people, account, invoice_factory):
    source(people, account, cycles[0], "AMBIGUOUS")
    source(people, account, cycles[1], "AMBIGUOUS")
    invoice = invoice_factory(number="AMBIGUOUS", amount="100", reconciled=False,
                              period_start=cycles[0].period_start, period_end=cycles[0].period_end)
    assert invoice.billing_cycle.is_unassigned
    assert not report(invoice)["valid"]
    assert not StatementRow.objects.filter(invoice=invoice, status=StatementRow.Status.MATCHED).exists()


def test_import_file_and_invoice_link_without_recapture(cycles, people, account):
    text = ("account_number,invoice_number,issue_date,due_date,Total Due,currency\n"
            f"{account.account_number},IMPORTED,{timezone.localdate()},{timezone.localdate()},100.000,MUR\n")
    source(people, account, cycles[0], "IMPORTED")
    batch = stage_spreadsheet(people["clerk"], ContentFile(text.encode(), name="upload.csv"),
                              account.provider)
    invoices, failed = commit(batch, people["clerk"])
    assert failed == 0 and len(invoices) == 1
    row = batch.rows.get()
    invoice = invoices[0]
    assert invoice.billing_cycle == cycles[0]
    assert row.billing_cycle_id == invoice.billing_cycle_id
    assert batch.billing_cycles.filter(pk=cycles[0].pk).exists()
    assert report(invoice)["valid"]


def test_calendar_counts_and_drills_do_not_leak_ministries(cycles, people, account, invoice_factory, tree, client):
    pending = invoice_factory(number="PENDING-CAL", billing_cycle=cycles[0], reconciled=False, due_in=-1)
    paid = invoice_factory(number="PAID-CAL", billing_cycle=cycles[1], reconciled=False)
    Invoice.objects.filter(pk=paid.pk).update(status=Invoice.Status.PAID)
    foreign = ServiceAccount.objects.create(provider=account.provider, org_unit=tree["moe"], account_number="SIDE")
    Invoice.objects.create(provider=account.provider, org_unit=tree["moe"], service_account=foreign,
        billing_cycle=cycles[0], invoice_number="PRIVATE-OTHER-MINISTRY", reference="PRIVATE-REF",
        issue_date=timezone.localdate(), due_date=timezone.localdate(), currency="MUR", amount_total=100,
        created_by=people["outsider"], utility_type=account.provider.utility_type)
    login(client, people["clerk"])
    response = client.get(reverse("providers:cycles"), {"month": cycles[0].period_end.strftime("%Y-%m")})
    assert response.status_code == 200
    assert b"Provider catalogue" not in response.content
    assert response.context["selected_provider"] == ""
    assert response.context["totals"] == {"received": 2, "processed": 1, "pending": 1, "overdue": 1}
    page = client.get(cycles[0].get_absolute_url() + "?status=pending")
    assert page.status_code == 200
    assert pending.reference.encode() in page.content
    assert b"PRIVATE-OTHER-MINISTRY" not in page.content
    page = client.get(cycles[1].get_absolute_url() + "?status=processed")
    assert paid.reference.encode() in page.content
    assert list(page.context["invoices"]) == [paid]
    assert client.post(reverse("providers:cycle_create"), {}).status_code == 403


def test_legacy_file_can_be_assigned_without_uploading_again(cycles, people, account, invoice_factory):
    statement = source(people, account, None, "LEGACY-LINK")
    invoice = invoice_factory(number="LEGACY-LINK", amount="100", reconciled=False)
    digest = statement.sha256
    assigned = assign_statement_cycle(statement, people["finance"], cycles[0])
    invoice.refresh_from_db()
    assert assigned.sha256 == digest
    assert invoice.billing_cycle == cycles[0]
    assert report(invoice)["valid"]


def test_changed_invoice_invalidates_auto_match(cycles, people, account, invoice_factory):
    source(people, account, cycles[0], "CHANGED")
    invoice = invoice_factory(number="CHANGED", amount="100", billing_cycle=cycles[0], reconciled=False)
    assert report(invoice)["valid"]
    invoice.amount_total = Decimal("125")
    save_invoice(invoice, people["clerk"])
    assert not report(invoice)["valid"]


def test_cycle_change_invalidates_match_without_rewriting_financial_snapshot(cycles, people, account, invoice_factory):
    statement = source(people, account, cycles[0], "CYCLE-MOVE")
    invoice = invoice_factory(number="CYCLE-MOVE", amount="100", billing_cycle=cycles[0], reconciled=False)
    before = invoice.snapshot()
    invoice.billing_cycle = cycles[1]
    save_invoice(invoice, people["clerk"])
    assert invoice.snapshot() == before
    assert statement.rows.get().status == StatementRow.Status.UNSORTED
    assert not report(invoice)["valid"]


def test_auto_match_does_not_take_a_row_assigned_outside_invoice_unit(cycles, people, account, invoice_factory, tree):
    statement = source(people, account, cycles[0], "FOREIGN-ASSIGNED")
    statement.rows.update(assigned_unit=tree["moe"])
    invoice = invoice_factory(number="FOREIGN-ASSIGNED", amount="100",
                              billing_cycle=cycles[0], reconciled=False)
    row = statement.rows.get()
    assert row.assigned_unit == tree["moe"]
    assert row.status != StatementRow.Status.MATCHED
    assert not report(invoice)["valid"]
