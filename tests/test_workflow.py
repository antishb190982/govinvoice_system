from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from invoices.models import Flag, Invoice
from org.models import Role
from signatures.services import verify_signature
from tests.conftest import PIN
from workflow import engine
from workflow.models import ApprovalPolicy, ApprovalStep, ApprovalTask


def pending(inv):
    return inv.approval_tasks.get(status=ApprovalTask.Status.PENDING)


def test_full_approval_with_signature(signers, policy, make_invoice):
    p = signers
    inv = make_invoice("1200.000")
    engine.submit(inv, p["clerk"])
    engine.decide(pending(inv), p["verifier"], engine.Decision.APPROVE)
    task = engine.decide(pending(inv), p["approver"], engine.Decision.APPROVE, pin=PIN)
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.APPROVED
    # Senior step skipped below 5,000.
    assert inv.approval_tasks.count() == 2
    ok, findings = verify_signature(task.signature)
    assert ok, findings


def test_signature_detects_later_change_to_invoice(signers, policy, make_invoice):
    p = signers
    inv = make_invoice()
    engine.submit(inv, p["clerk"])
    engine.decide(pending(inv), p["verifier"], engine.Decision.APPROVE)
    task = engine.decide(pending(inv), p["approver"], engine.Decision.APPROVE, pin=PIN)
    Invoice.objects.filter(pk=inv.pk).update(amount_total=Decimal("9999"))  # tampering behind the app
    task.refresh_from_db()
    ok, findings = verify_signature(task.signature)
    assert not ok and any("changed" in f for f in findings)


def test_high_value_needs_extra_step_by_different_person(signers, policy, make_invoice):
    p = signers
    inv = make_invoice("8000.000")
    engine.submit(inv, p["clerk"])
    engine.decide(pending(inv), p["verifier"], engine.Decision.APPROVE)
    engine.decide(pending(inv), p["approver"], engine.Decision.APPROVE, pin=PIN)
    with pytest.raises(PermissionDenied):
        engine.decide(pending(inv), p["approver"], engine.Decision.APPROVE, pin=PIN)
    engine.decide(pending(inv), p["approver2"], engine.Decision.APPROVE, pin=PIN)
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.APPROVED


def test_creator_cannot_approve_even_with_role(signers, policy, make_invoice, tree):
    p = signers
    from org.models import Membership

    Membership.objects.create(user=p["clerk"], org_unit=tree["moh"], role=Role.VERIFIER)
    inv = make_invoice()
    engine.submit(inv, p["clerk"])
    with pytest.raises(PermissionDenied):
        engine.decide(pending(inv), p["clerk"], engine.Decision.APPROVE)


def test_wrong_role_or_unit_cannot_decide(signers, policy, make_invoice):
    p = signers
    inv = make_invoice()
    engine.submit(inv, p["clerk"])
    with pytest.raises(PermissionDenied):
        engine.decide(pending(inv), p["approver"], engine.Decision.APPROVE)  # step 1 needs verifier
    with pytest.raises(PermissionDenied):
        engine.decide(pending(inv), p["outsider"], engine.Decision.APPROVE)


def test_wrong_pin_rejected_and_locks(signers, policy, make_invoice, settings):
    p = signers
    inv = make_invoice()
    engine.submit(inv, p["clerk"])
    engine.decide(pending(inv), p["verifier"], engine.Decision.APPROVE)
    for _ in range(settings.SIGNING_PIN_MAX_FAILURES):
        with pytest.raises(ValidationError):
            engine.decide(pending(inv), p["approver"], engine.Decision.APPROVE, pin="wrong-pin-123")
    with pytest.raises(PermissionDenied):
        engine.decide(pending(inv), p["approver"], engine.Decision.APPROVE, pin=PIN)
    assert pending(inv).order == 2


def test_return_and_resubmit_starts_new_round(signers, policy, make_invoice):
    p = signers
    inv = make_invoice()
    engine.submit(inv, p["clerk"])
    with pytest.raises(ValidationError):
        engine.decide(pending(inv), p["verifier"], engine.Decision.RETURN, "")  # reason required
    engine.decide(pending(inv), p["verifier"], engine.Decision.RETURN, "Wrong meter reading")
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.RETURNED and not inv.approval_tasks.filter(status="waiting").exists()
    engine.submit(inv, p["clerk"])
    assert pending(inv).round == 2


def test_submit_requires_document(people, policy, account):
    from invoices.services import save_invoice

    inv = save_invoice(Invoice(service_account=account, invoice_number="NODOC", issue_date=timezone.localdate(),
                               due_date=timezone.localdate() + timedelta(days=30), amount_total=10), people["clerk"])
    with pytest.raises(ValidationError):
        engine.submit(inv, people["clerk"])


def test_policy_specificity(tree, policy, make_invoice):
    local = ApprovalPolicy.objects.create(name="Hospital electricity", org_unit=tree["moh"], utility_type="electricity")
    ApprovalStep.objects.create(policy=local, order=1, name="Only", role=Role.APPROVER)
    ApprovalPolicy.objects.create(name="Health any", org_unit=tree["moh"])
    inv = make_invoice()
    assert engine.select_policy(inv) == local


def test_sla_never_runs_past_settlement_deadline(signers, policy, make_invoice):
    inv = make_invoice(due_in=5)
    engine.submit(inv, signers["clerk"])
    task = pending(inv)
    inv.refresh_from_db()
    assert task.due_at <= inv.must_approve_by


def test_late_invoice_is_expedited_and_flagged(signers, policy, make_invoice):
    inv = make_invoice(due_in=1)
    assert Flag.LATE_ARRIVAL in inv.flags
    engine.submit(inv, signers["clerk"])
    task = pending(inv)
    assert task.expedited
    from core.worktime import working_hours_between

    assert working_hours_between(task.activated_at, task.due_at) <= engine.EXPEDITED_HOURS + 0.01


def test_breach_escalates_to_escalation_role(signers, policy, make_invoice, tree):
    p = signers
    inv = make_invoice()
    engine.submit(inv, p["clerk"])
    task = pending(inv)
    ApprovalTask.objects.filter(pk=task.pk).update(due_at=timezone.now() - timedelta(hours=1))
    with pytest.raises(PermissionDenied):
        engine.check_can_decide(pending(inv), p["admin"])
    reminded, escalated = engine.sweep_sla()
    assert escalated == 1
    task = pending(inv)
    assert task.breached and task.escalated_at
    engine.check_can_decide(task, p["admin"])  # unit admin may now act
    assert p["admin"].notifications.filter(title__startswith="SLA breached").exists()
    assert pending(inv) in engine.pending_for(p["admin"])


def test_severe_flags_need_justification(signers, policy, make_invoice):
    p = signers
    inv = make_invoice("100.000", amount_net=Decimal("50"), amount_tax=Decimal("5"))
    assert Flag.TOTAL_MISMATCH in inv.flags
    engine.submit(inv, p["clerk"])
    with pytest.raises(ValidationError):
        engine.decide(pending(inv), p["verifier"], engine.Decision.APPROVE, "")
    engine.decide(pending(inv), p["verifier"], engine.Decision.APPROVE, "Provider confirmed rounding adjustment")


def test_duplicate_invoice_rejected(make_invoice):
    make_invoice(number="DUP-1")
    with pytest.raises(ValidationError):
        make_invoice(number="dup-1")


def test_spike_detection(make_invoice):
    for _ in range(4):
        inv = make_invoice("1000.000")
    spike = make_invoice("2000.000")
    assert Flag.AMOUNT_SPIKE in spike.flags
    assert Flag.AMOUNT_SPIKE not in inv.flags
