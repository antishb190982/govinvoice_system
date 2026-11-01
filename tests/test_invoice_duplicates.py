import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from imports.models import ImportRow
from imports.services import commit, recheck, stage_spreadsheet, update_row
from invoices.forms import InvoiceForm
from invoices.models import Invoice
from invoices.services import save_invoice
from providers.models import Provider, ServiceAccount

pytestmark = pytest.mark.django_db


def period():
    today = timezone.localdate()
    return {"period_start": today - timedelta(days=32), "period_end": today - timedelta(days=3)}


def candidate(account, number="OTHER-NUMBER", amount="1000", **extra):
    today = timezone.localdate()
    return Invoice(service_account=account, invoice_number=number,
                   issue_date=today - timedelta(days=2), due_date=today + timedelta(days=30),
                   amount_total=Decimal(amount), **extra)


def stage(people, account, numbers=("NEW-A", "NEW-B")):
    today = timezone.localdate()
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "account_number", "invoice_number", "issue_date", "due_date",
        "period_start", "period_end", "currency", "amount_total",
    ])
    writer.writerow({field: "Total Due" if field == "amount_total" else field
                     for field in writer.fieldnames})
    for number in numbers:
        writer.writerow(dict(account_number=account.account_number, invoice_number=number,
                             issue_date=today - timedelta(days=2), due_date=today + timedelta(days=30),
                             currency="MUR", amount_total="1000.000", **period()))
    return stage_spreadsheet(
        people["clerk"], SimpleUploadedFile("duplicate-check.csv", output.getvalue().encode()),
        account.provider,
    )


@pytest.mark.parametrize("number", ["ORIGINAL", "original", " Original "])
def test_reference_rejected_regardless_of_case_or_spaces(make_invoice, people, account, number):
    make_invoice(number="ORIGINAL")
    with pytest.raises(ValidationError, match="already recorded"):
        save_invoice(candidate(account, number), people["clerk"])
    assert Invoice.objects.count() == 1


@pytest.mark.parametrize("status", [
    Invoice.Status.DRAFT, Invoice.Status.RETURNED, Invoice.Status.IN_APPROVAL,
    Invoice.Status.APPROVED, Invoice.Status.SCHEDULED, Invoice.Status.PAID, Invoice.Status.DISPUTED,
])
def test_period_amount_duplicate_rejected_even_with_different_number(
        make_invoice, people, account, status):
    original = make_invoice(number="ORIGINAL", **period())
    Invoice.objects.filter(pk=original.pk).update(status=status)
    with pytest.raises(ValidationError, match="billing period"):
        save_invoice(candidate(account, amount="1000.000", **period()), people["clerk"])
    assert Invoice.objects.count() == 1


def test_no_false_rejection_for_different_period_account_amount_currency(
        make_invoice, people, account, tree):
    make_invoice(number="ORIGINAL", **period())
    other_account = ServiceAccount.objects.create(
        provider=account.provider, org_unit=tree["hosp"], account_number="DIFFERENT",
    )
    variants = [
        candidate(other_account, "OTHER-ACCOUNT", **period()),
        candidate(account, "OTHER-AMOUNT", amount="1001", **period()),
        candidate(account, "OTHER-CURRENCY", currency="USD", **period()),
        candidate(account, "OTHER-PERIOD", period_start=timezone.localdate() - timedelta(days=63),
                  period_end=timezone.localdate() - timedelta(days=33)),
    ]
    for invoice in variants:
        save_invoice(invoice, people["clerk"])
    assert Invoice.objects.count() == 5


@pytest.mark.parametrize("utility", ["electricity", "water"])
def test_different_provider_may_use_same_number_account_and_period(make_invoice, people, tree, utility):
    make_invoice(number="ORIGINAL", **period())
    provider = Provider.objects.create(name="Different Utility", short_code="OTHER", utility_type=utility)
    account = ServiceAccount.objects.create(provider=provider, org_unit=tree["hosp"], account_number="ACC-1")
    assert save_invoice(candidate(account, "ORIGINAL", **period()), people["clerk"]).pk


def test_missing_period_does_not_reject_recurring_amounts(make_invoice, people, account):
    make_invoice(number="ORIGINAL")
    assert save_invoice(candidate(account), people["clerk"]).pk


def test_cancelled_bill_allows_new_reference_but_reserves_original(make_invoice, people, account):
    old = make_invoice(number="ORIGINAL", **period())
    Invoice.objects.filter(pk=old.pk).update(status=Invoice.Status.CANCELLED)
    assert save_invoice(candidate(account, **period()), people["clerk"]).pk
    with pytest.raises(ValidationError, match="already recorded"):
        save_invoice(candidate(account, "ORIGINAL", amount="25"), people["clerk"])


def test_edit_excludes_self_but_cannot_turn_another_bill_into_duplicate(make_invoice, people):
    first = make_invoice(number="FIRST", **period())
    assert save_invoice(first, people["clerk"]).pk == first.pk
    second = make_invoice(number="SECOND", amount="2000", **period())
    second.amount_total = Decimal("1000")
    with pytest.raises(ValidationError, match="Duplicate bill rejected"):
        save_invoice(second, people["clerk"])
    second.refresh_from_db()
    assert second.amount_total == Decimal("2000")


def test_canonical_account_numbers_match_legacy_case_and_spaces(make_invoice, people, account, tree):
    make_invoice(number="ORIGINAL", **period())
    legacy = ServiceAccount.objects.create(
        provider=account.provider, org_unit=tree["hosp"], account_number=" acc-1 ",
    )
    with pytest.raises(ValidationError, match="account number"):
        save_invoice(candidate(legacy, **period()), people["clerk"])


def test_manual_form_shows_duplicate_error(make_invoice, people, account):
    make_invoice(number="ORIGINAL", **period())
    data = {"service_account": account.pk, "invoice_number": "NEW", "amount_total": "1000",
            "issue_date": timezone.localdate() - timedelta(days=2), "received_date": timezone.localdate(),
            "due_date": timezone.localdate() + timedelta(days=30), "currency": "MUR", **period()}
    form = InvoiceForm(data=data, user=people["clerk"])
    assert not form.is_valid()
    assert "Duplicate bill rejected" in str(form.errors["amount_total"])


def test_staged_duplicates_are_invalid_and_only_one_invoice_is_committed(people, account):
    batch = stage(people, account)
    first, second = list(batch.rows.all())
    assert first.status == ImportRow.Status.VALID
    assert second.status == ImportRow.Status.INVALID
    assert "duplicate of row" in " ".join(second.errors)
    created, failed = commit(batch, people["clerk"])
    assert len(created) == 1 and failed == 0
    assert Invoice.objects.count() == 1


def test_existing_bill_rejected_while_staging(make_invoice, people, account):
    make_invoice(number="EXISTING", **period())
    batch = stage(people, account, numbers=("NEW",))
    row = batch.rows.get()
    assert row.status == ImportRow.Status.INVALID
    assert "Duplicate bill rejected" in " ".join(row.errors)


def test_new_bill_after_staging_is_caught_at_commit(make_invoice, people, account):
    batch = stage(people, account, numbers=("NEW",))
    make_invoice(number="ARRIVED-LATER", **period())
    created, failed = commit(batch, people["clerk"])
    assert not created and failed == 1
    row = batch.rows.get()
    assert row.status == ImportRow.Status.INVALID
    assert Invoice.objects.count() == 1


def test_excluded_first_row_does_not_block_valid_selected_copy(people, account):
    batch = stage(people, account)
    first, second = list(batch.rows.all())
    first.include = False
    first.save(update_fields=["include"])
    recheck(batch, people["clerk"])
    second.refresh_from_db()
    assert second.status == ImportRow.Status.VALID
    created, _ = commit(batch, people["clerk"])
    assert [item.invoice_number for item in created] == ["NEW-B"]


def test_editing_staged_amount_removes_previous_duplicate_marker(people, account):
    batch = stage(people, account)
    first, second = list(batch.rows.all())
    update_row(first, people["clerk"], {"amount_total": "2000"})
    second.refresh_from_db()
    assert second.status == ImportRow.Status.VALID
    created, _ = commit(batch, people["clerk"])
    assert len(created) == 2


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1000.0001"])
def test_invalid_precision_cannot_evade_duplicate_identity(make_invoice, people, account, amount):
    make_invoice(number="ORIGINAL", **period())
    with pytest.raises(ValidationError):
        save_invoice(candidate(account, amount=amount, **period()), people["clerk"])
    assert Invoice.objects.count() == 1


def test_provider_lock_is_acquired_before_duplicate_validation(monkeypatch, people, account):
    from django.db import connection
    from invoices import services

    events = []
    manager = Provider.objects
    original_lock, original_prepare = manager.select_for_update, services.prepare

    def locked(*args, **kwargs):
        assert connection.in_atomic_block
        events.append("lock")
        return original_lock(*args, **kwargs)

    def prepared(invoice):
        events.append("validate")
        return original_prepare(invoice)

    monkeypatch.setattr(manager, "select_for_update", locked)
    monkeypatch.setattr(services, "prepare", prepared)
    save_invoice(candidate(account), people["clerk"])
    assert events[:2] == ["lock", "validate"]
