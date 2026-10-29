from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from accounts.middleware import OTP_SESSION_KEY
from core.models import AuditEvent
from invoices.models import Invoice
from operations import services as ops
from operations.models import BatchMember, DisputeCase, InvoiceBatch
from org.models import Role
from providers.models import Provider, ServiceAccount
from reconciliation import services as recon
from settlements import services as settle
from workflow import engine
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def pin_auth(monkeypatch):
    # PIN verification itself remains covered by original signing/security tests.
    monkeypatch.setattr("signatures.services.check_pin", lambda user, pin: None)


def case(people, invoice, kind="waiver", amount="100"):
    return ops.open_case(people["clerk"], invoice, kind, "Verify the billed charges", Decimal(amount))


def authorise(people, item):
    item = ops.endorse(item, people["finance"], "Checked supporting invoice")
    return ops.decide_case(item, people["authoriser"], True, "Approved independently", "test-pin")


def approved(invoice):
    invoice.status = Invoice.Status.APPROVED
    invoice.save()


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def test_batch_single_provider_ministry_and_atomic_processing(make_invoice, tree, people, policy):
    first, second = make_invoice(), make_invoice()
    batch = ops.create_batch(people["admin"], tree["moh"], "Monthly power", [first.pk, second.pk])
    assert batch.ministry == tree["moh"] and batch.members.count() == 2
    ops.start_batch(batch, people["admin"])
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == second.status == Invoice.Status.IN_APPROVAL
    assert AuditEvent.objects.filter(action="batch.submitted", object_id=str(batch.pk)).exists()


def test_cross_ministry_and_mixed_provider_batches_rejected(make_invoice, tree, people, provider):
    first = make_invoice()
    other_account = ServiceAccount.objects.create(provider=provider, org_unit=tree["moe"], account_number="EDU")
    foreign = make_invoice()
    foreign.service_account, foreign.org_unit = other_account, other_account.org_unit
    foreign.save()
    with pytest.raises(ValidationError, match="one ministry"):
        ops.create_batch(people["admin"], tree["gov"], "Mixed ministry", [first.pk, foreign.pk])
    other_provider = Provider.objects.create(name="Water", short_code="WATER", utility_type="water", currency="MUR")
    water_account = ServiceAccount.objects.create(provider=other_provider, org_unit=first.org_unit, account_number="W1")
    water = make_invoice()
    water.service_account, water.provider = water_account, other_provider
    water.save()
    with pytest.raises(ValidationError, match="one utility"):
        ops.create_batch(people["admin"], tree["moh"], "Mixed providers", [first.pk, water.pk])
    assert not InvoiceBatch.objects.exists()


def test_ministry_admin_cannot_process_other_ministry(make_invoice, tree, people):
    invoice = make_invoice()
    outsider = make_user("education-admin", [(Role.UNIT_ADMIN, tree["moe"])])
    with pytest.raises(PermissionDenied):
        ops.create_batch(outsider, tree["moh"], "Forbidden", [invoice.pk])
    batch = ops.create_batch(people["admin"], tree["moh"], "Health", [invoice.pk])
    with pytest.raises(PermissionDenied):
        ops.start_batch(batch, outsider)


def test_batch_membership_no_duplicates_cancel_retains_history(make_invoice, people, tree):
    invoice = make_invoice()
    batch = ops.create_batch(people["clerk"], tree["moh"], "Original", [invoice.pk])
    with pytest.raises(ValidationError, match="active batch"):
        ops.create_batch(people["clerk"], tree["moh"], "Duplicate", [invoice.pk])
    ops.cancel_batch(batch, people["clerk"], "Regroup the draft invoices")
    assert batch.members.count() == 1 and not batch.members.get().active
    replacement = ops.create_batch(people["clerk"], tree["moh"], "Replacement", [invoice.pk])
    assert BatchMember.objects.filter(invoice=invoice).count() == 2 and replacement.pk != batch.pk


def test_batch_submission_rolls_back_when_one_invoice_on_dispute(make_invoice, people, tree, policy):
    first, second = make_invoice(), make_invoice()
    batch = ops.create_batch(people["clerk"], tree["moh"], "Atomic", [first.pk, second.pk])
    case(people, second, kind="issue", amount="0")
    with pytest.raises(ValidationError, match="dispute hold"):
        ops.start_batch(batch, people["clerk"])
    first.refresh_from_db()
    assert first.status == "draft" and first.approval_tasks.count() == 0


def test_dispute_blocks_approval_and_settlement(make_invoice, people, tree, policy):
    invoice = make_invoice()
    tasks = engine.submit(invoice, people["clerk"])
    case(people, invoice, kind="issue", amount="0")
    with pytest.raises(ValidationError, match="dispute hold"):
        engine.decide(tasks[0], people["verifier"], engine.Decision.APPROVE)
    approved(invoice)
    assert not settle.eligible_invoices(tree["moh"], "MUR", invoice.due_date).exists()


def test_item_waiver_is_independent_and_reapproved_at_net_amount(make_invoice, people, tree):
    invoice = make_invoice()
    approved(invoice)
    item = ops.open_case(people["clerk"], invoice, "waiver", "Late charge incorrect", Decimal("100"),
                        "Late payment charge", Decimal("100"))
    ops.endorse(item, people["finance"], "Charge verified")
    with pytest.raises(PermissionDenied):
        ops.decide_case(item, people["finance"], True, "Cannot approve own review", "x")
    item = ops.decide_case(item, people["authoriser"], True, "Waive this charge", "x")
    invoice.refresh_from_db()
    assert invoice.amount_total == Decimal("1000") and ops.payable(invoice) == Decimal("900")
    assert invoice.status == "returned" and item.applies_payable
    assert not recon.report(invoice)["valid"]
    recon.recheck(invoice.reconciliation_rows.get(), people["finance"])
    assert recon.report(invoice)["valid"]
    approved(invoice)
    instruction = settle.create_instruction(people["finance"], tree["moh"], "MUR", invoice.due_date, [invoice.pk])
    assert instruction.total_amount == Decimal("900")
    assert instruction.lines.get().items.get().amount == Decimal("900")
    recon.check_instruction(instruction)


def test_whole_batch_settled_not_partial_and_provider_stays_single(make_invoice, people, tree):
    first, second = make_invoice(), make_invoice()
    approved(first)
    approved(second)
    batch = ops.create_batch(people["finance"], tree["moh"], "One provider", [first.pk, second.pk])
    with pytest.raises(ValidationError, match="entire batch"):
        settle.create_instruction(people["finance"], tree["moh"], "MUR", first.due_date, [first.pk])
    instruction = settle.create_instruction(people["finance"], tree["moh"], "MUR",
        max(first.due_date, second.due_date), [first.pk, second.pk])
    assert instruction.batch == batch and instruction.lines.count() == 1
    assert instruction.total_amount == first.amount_total + second.amount_total


def test_dispute_after_preparation_blocks_release_and_waiver_requires_cancellation(make_invoice, people, tree):
    invoice = make_invoice()
    approved(invoice)
    instruction = settle.create_instruction(people["finance"], tree["moh"], "MUR", invoice.due_date, [invoice.pk])
    invoice.refresh_from_db()
    item = case(people, invoice)
    with pytest.raises(ValidationError, match="dispute hold"):
        recon.check_instruction(instruction)
    ops.endorse(item, people["finance"], "Waiver requested")
    with pytest.raises(ValidationError, match="Cancel"):
        ops.decide_case(item, people["authoriser"], True, "Approved", "x")


def test_paid_refund_partial_receipts_and_history_preserved(make_invoice, people):
    invoice = make_invoice()
    invoice.status, invoice.payment_reference, invoice.paid_at = "paid", "ORIGINAL-PAYMENT", timezone.localdate()
    invoice.save()
    invoice.refresh_from_db()
    original = invoice.snapshot()
    item = authorise(people, case(people, invoice, kind="refund", amount="100"))
    assert item.status == "refund_due"
    ops.record_refund(item, people["finance"], "REF-PART-1", timezone.localdate(),
                      ContentFile(b"%PDF-receipt", name="receipt.pdf"), Decimal("40"))
    item.refresh_from_db()
    assert item.status == "refund_due"
    ops.record_refund(item, people["finance"], "REF-PART-2", timezone.localdate(),
                      ContentFile(b"%PDF-receipt", name="receipt.pdf"), Decimal("60"))
    item.refresh_from_db()
    invoice.refresh_from_db()
    assert item.status == "refunded" and item.refund_receipts.count() == 2
    assert invoice.snapshot() == original and invoice.status == "paid" and invoice.payment_reference == "ORIGINAL-PAYMENT"


def test_paid_waiver_becomes_refund_not_rewritten_payment(make_invoice, people):
    invoice = make_invoice()
    invoice.status = "paid"
    invoice.save()
    item = authorise(people, case(people, invoice))
    assert item.status == "refund_due" and not item.applies_payable
    assert ops.payable(invoice) == invoice.amount_total


def test_over_adjustment_and_double_refund_reference_are_blocked(make_invoice, people):
    invoice = make_invoice()
    with pytest.raises(ValidationError):
        case(people, invoice, amount="1001")
    with pytest.raises(ValidationError):
        ops.open_case(people["clerk"], invoice, "waiver", "Bad item", Decimal("150"), "Fee", Decimal("100"))
    invoice.status = "paid"
    invoice.save()
    item = authorise(people, case(people, invoice))
    ops.record_refund(item, people["finance"], "DUPLICATE-REF", timezone.localdate(),
                      ContentFile(b"%PDF-proof", name="receipt.pdf"), Decimal("40"))
    with pytest.raises(ValidationError):
        ops.record_refund(item, people["finance"], "DUPLICATE-REF", timezone.localdate(),
            ContentFile(b"%PDF-proof", name="receipt.pdf"), Decimal("20"))
    with pytest.raises(ValidationError):
        ops.record_refund(item, people["finance"], "TOO-MUCH", timezone.localdate(),
            ContentFile(b"%PDF-proof", name="receipt.pdf"), Decimal("100"))
    assert item.refund_receipts.count() == 1


@pytest.mark.parametrize("status", ["on_hold", "decommissioned", "dispute", "suspended"])
def test_service_status_holds_processing_but_invoice_remains_recorded(make_invoice, people, status):
    invoice = make_invoice()
    account = invoice.service_account
    account.status = status
    account.save()
    invoice.refresh_from_db()
    with pytest.raises(ValidationError, match="service account"):
        engine.submit(invoice, people["clerk"])
    assert invoice.status == "draft" and not recon.report(invoice)["valid"]
    account.status, account.is_active = "active", True
    account.save()
    assert recon.report(invoice)["valid"]


def test_frontend_audit_and_scope(client, people, tree, make_invoice):
    invoice = make_invoice()
    batch = ops.create_batch(people["clerk"], tree["moh"], "UI batch", [invoice.pk])
    item = case(people, invoice, kind="issue", amount="0")
    login(client, people["clerk"])
    page = client.get(batch.get_absolute_url())
    assert page.status_code == 200
    class ShellParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.main_in_shell = [], False
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "main" and attrs.get("id") == "content":
                self.main_in_shell = any("shell-main" in a.get("class", "").split() for _, a in self.stack)
            if tag not in {"input", "img", "meta", "link", "br", "hr", "area", "base", "col", "embed", "param", "source", "track", "wbr"}:
                self.stack.append((tag, attrs))
        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break
    parsed = ShellParser()
    parsed.feed(page.content.decode())
    assert parsed.main_in_shell, "Stray closing tags must not move content outside the sidebar's offset column."
    assert b"batch.created" in client.get(batch.get_absolute_url()).content
    assert client.get(item.get_absolute_url()).status_code == 200
    assert client.get(reverse("operations:cases")).status_code == 200
    assert client.get(reverse("providers:accounts")).status_code == 200
    login(client, make_user("education-viewer", [(Role.FINANCE, tree["moe"])]))
    assert client.get(batch.get_absolute_url()).status_code == 404
    assert client.get(item.get_absolute_url()).status_code == 404


def test_stale_review_requires_finance_refresh(make_invoice, people):
    invoice = make_invoice()
    item = case(people, invoice)
    ops.endorse(item, people["finance"], "Initial review")
    invoice.invoice_number += "-CORRECTED"
    invoice.save()
    with pytest.raises(ValidationError, match="refresh"):
        ops.decide_case(item, people["authoriser"], True, "Approve", "x")
    ops.endorse(item, people["finance"], "Rechecked corrected invoice")
    assert ops.decide_case(item, people["authoriser"], True, "Approve", "x").applies_payable


def test_same_item_cannot_be_adjusted_twice_beyond_original_charge(make_invoice, people):
    invoice = make_invoice()
    first = ops.open_case(people["clerk"], invoice, "waiver", "Fee", Decimal("60"), "Line 3 - fee", Decimal("100"))
    authorise(people, first)
    second = ops.open_case(people["clerk"], invoice, "waiver", "Fee", Decimal("60"), "LINE 3 - FEE", Decimal("100"))
    ops.endorse(second, people["finance"], "Second review")
    with pytest.raises(ValidationError, match="original amount"):
        ops.decide_case(second, people["authoriser"], True, "Approve", "x")
    assert ops.payable(invoice) == Decimal("940")


def test_release_and_regroup_retains_former_batch_and_approval_trail(make_invoice, people, tree, policy):
    from operations.regroup import release_member
    invoice = make_invoice()
    batch = ops.create_batch(people["clerk"], tree["moh"], "Before correction", [invoice.pk])
    ops.start_batch(batch, people["clerk"])
    task = invoice.approval_tasks.get(status="pending")
    engine.decide(task, people["verifier"], engine.Decision.RETURN, "Correct the coding")
    release_member(batch, people["clerk"], invoice.pk, "Move to a corrected batch")
    assert not batch.members.get().active
    replacement = ops.create_batch(people["clerk"], tree["moh"], "After correction", [invoice.pk])
    assert invoice.batch_memberships.count() == 2
    assert "released" in batch.lifecycle and replacement.lifecycle == "Ready to process"
    assert invoice.approval_tasks.exists()


def test_release_not_allowed_for_live_settlement(make_invoice, people, tree):
    from operations.regroup import release_member
    invoice = make_invoice()
    approved(invoice)
    instruction = settle.create_instruction(people["finance"], tree["moh"], "MUR", invoice.due_date, [invoice.pk])
    with pytest.raises(ValidationError, match="Cancel"):
        release_member(instruction.batch, people["finance"], invoice.pk, "Cannot release yet")


def test_account_metadata_update_is_scoped_and_audited(client, people, tree, account):
    login(client, people["admin"])
    response = client.post(reverse("providers:account_edit", args=[account.pk]), {
        "provider": account.provider_id, "org_unit": account.org_unit_id, "account_number": account.account_number,
        "status": "suspended", "service_type": "Hospital power", "purpose": "Clinical operations",
        "owner": "Facilities team", "service_details": "Main ward connection"})
    assert response.status_code == 302
    account.refresh_from_db()
    assert account.status == "suspended" and account.owner == "Facilities team" and not account.is_active
    assert AuditEvent.objects.filter(action="service_account.saved", object_id=str(account.pk),
                                     actor=people["admin"]).exists()
    login(client, make_user("other-unit-admin", [(Role.UNIT_ADMIN, tree["moe"])]))
    assert client.get(reverse("providers:account_edit", args=[account.pk])).status_code == 403


def test_fully_waived_member_stays_in_batch_but_does_not_block_cash_settlement(make_invoice, people, tree):
    waived, cash = make_invoice(), make_invoice()
    approved(waived)
    approved(cash)
    batch = ops.create_batch(people["finance"], tree["moh"], "Mixed nil and cash", [waived.pk, cash.pk])
    authorise(people, case(people, waived, amount="1000"))
    waived.refresh_from_db()
    with pytest.raises(ValidationError, match="zero-payable"):
        settle.create_instruction(people["finance"], tree["moh"], "MUR", cash.due_date, [cash.pk])
    waived.status, waived.payment_reference = "paid", "NIL – nothing to pay"
    waived.save()
    instruction = settle.create_instruction(people["finance"], tree["moh"], "MUR", cash.due_date, [cash.pk])
    assert instruction.batch == batch and instruction.total_amount == Decimal("1000")
    assert batch.members.filter(active=True).count() == 2 and instruction.lines.get().items.count() == 1
