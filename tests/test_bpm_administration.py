from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import OTP_SESSION_KEY
from accounts.models import User
from core.models import AuditEvent
from invoices.models import Invoice
from org.models import Membership, Role
from tests.conftest import make_user
from workflow import engine
from workflow.configuration import policy_version
from workflow.models import ApprovalPolicy, ApprovalStep, ApprovalTask
from workflow.process import invoice_process

pytestmark = pytest.mark.django_db


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def policy_data(unit, policy=None, **extra):
    data = {
        "name": "BPM test workflow", "org_unit": unit.pk, "utility_type": "", "min_amount": "0",
        "max_amount": "", "priority": "50", "is_active": "on",
        "version": policy_version(policy) if policy else "",
        "steps-TOTAL_FORMS": "2", "steps-INITIAL_FORMS": "0",
        "steps-MIN_NUM_FORMS": "1", "steps-MAX_NUM_FORMS": "20",
    }
    for i, (name, role, signed) in enumerate([("Check documents", Role.VERIFIER, False),
                                             ("Signed authorisation", Role.APPROVER, True)]):
        data.update({f"steps-{i}-order": str(i + 1), f"steps-{i}-name": name,
                     f"steps-{i}-role": role, f"steps-{i}-sla_hours": "8",
                     f"steps-{i}-min_amount": "0", f"steps-{i}-escalation_role": Role.UNIT_ADMIN})
        if signed:
            data[f"steps-{i}-requires_signature"] = "on"
    data.update(extra)
    return data


@pytest.fixture
def local_admin(tree):
    return make_user("local-admin", [(Role.UNIT_ADMIN, tree["moh"])])


def test_admin_creates_user_with_initial_scoped_role_atomically(client, local_admin, tree):
    login(client, local_admin)
    password = "Initial-BPM-Pass-2026!"
    response = client.post(reverse("accounts:user_create"), {
        "username": "bpm-new", "full_name": "New BPM User", "email": "bpm@example.test",
        "temporary_password": password, "initial_unit": tree["hosp"].pk,
        "initial_role": Role.VERIFIER, "is_superuser": "on", "is_staff": "on",
    })
    assert response.status_code == 302
    user = User.objects.get(username="bpm-new")
    assert user.check_password(password) and user.must_change_password
    assert not user.is_superuser and not user.is_staff
    assert user.memberships.get().org_unit == tree["hosp"]
    assert user.memberships.get().role == Role.VERIFIER
    assert client.get(response.url).status_code == 200
    assert not AuditEvent.objects.filter(data__icontains=password).exists()


def test_creation_rejects_foreign_unit_and_unknown_permission_role(client, local_admin, tree):
    login(client, local_admin)
    for unit, role in [(tree["moe"], Role.VERIFIER), (tree["moh"], "superuser")]:
        result = client.post(reverse("accounts:user_create"), {
            "username": "forged-user", "full_name": "Forged", "temporary_password": "Initial-BPM-Pass-2026!",
            "initial_unit": unit.pk, "initial_role": role,
        })
        assert result.status_code == 200 and result.context["form"].errors
        assert not User.objects.filter(username="forged-user").exists()


def test_shared_user_profile_protected_but_local_role_expiry_is_manageable(
        client, local_admin, people, tree):
    shared = people["finance"]
    Membership.objects.create(user=shared, org_unit=tree["moe"], role=Role.FINANCE)
    login(client, local_admin)
    url = reverse("accounts:user_detail", args=[shared.pk])
    response = client.get(url)
    assert not response.context["can_edit_profile"]
    assert all(m.org_unit_id != tree["moe"].pk for m in response.context["memberships"])
    assert client.post(url, {"form": "edit", "full_name": "Changed", "is_active": ""}).status_code == 403
    assert client.post(url, {"form": "reset_2fa"}).status_code == 403
    expires = timezone.localdate() + timedelta(days=60)
    result = client.post(url, {"form": "role", "org_unit": tree["moh"].pk,
                               "role": Role.FINANCE, "valid_until": expires.isoformat()})
    assert result.status_code == 302
    assert shared.memberships.get(org_unit=tree["moh"], role=Role.FINANCE).valid_until == expires
    shared.refresh_from_db()
    assert shared.is_active and shared.full_name != "Changed"


def test_orphan_accounts_not_exposed_to_other_unit_administrators(client, local_admin):
    orphan = make_user("orphan")
    login(client, local_admin)
    assert client.get(reverse("accounts:user_detail", args=[orphan.pk])).status_code == 404


def test_admin_creates_ordered_signed_workflow_and_can_view_bpm(client, local_admin, tree):
    login(client, local_admin)
    result = client.post(reverse("workflow:policy_new"), policy_data(tree["moh"]))
    assert result.status_code == 302
    policy = ApprovalPolicy.objects.get(name="BPM test workflow")
    assert list(policy.steps.values_list("role", flat=True)) == [Role.VERIFIER, Role.APPROVER]
    assert policy.steps.last().requires_signature
    screen = client.get(result.url)
    assert screen.status_code == 200 and len(screen.context["nodes"]) == 2
    assert AuditEvent.objects.filter(action="workflow.policy_saved", object_id=str(policy.pk)).exists()


def test_workflow_editor_requires_admin_scope_and_retains_readonly_inherited_policy(
        client, local_admin, people, policy, tree):
    login(client, people["clerk"])
    assert client.get(reverse("workflow:policy_new")).status_code == 403
    login(client, local_admin)
    assert client.get(reverse("workflow:policy", args=[policy.pk])).status_code == 200
    assert client.get(reverse("workflow:policy_edit", args=[policy.pk])).status_code == 404
    result = client.post(reverse("workflow:policy_new"), policy_data(tree["moe"]))
    assert result.status_code == 200 and result.context["form"].errors
    assert not ApprovalPolicy.objects.filter(name="BPM test workflow").exists()


@pytest.mark.parametrize("field,value", [
    ("steps-1-order", "1"), ("steps-1-sla_hours", "0"), ("steps-1-role", Role.AUDITOR),
    ("steps-1-min_amount", "-1"), ("max_amount", "-1"),
])
def test_invalid_workflow_configuration_is_not_saved(client, local_admin, tree, field, value):
    login(client, local_admin)
    result = client.post(reverse("workflow:policy_new"), policy_data(tree["moh"], **{field: value}))
    assert result.status_code == 200
    assert not ApprovalPolicy.objects.filter(name="BPM test workflow").exists()


def test_active_workflow_cannot_leave_lower_amounts_without_signature(client, local_admin, tree):
    login(client, local_admin)
    result = client.post(reverse("workflow:policy_new"),
                         policy_data(tree["moh"], **{"steps-1-min_amount": "5000"}))
    assert result.status_code == 200 and result.context["form"].non_field_errors()
    assert not ApprovalPolicy.objects.filter(name="BPM test workflow").exists()


def test_policy_edit_does_not_rewrite_running_invoice_tasks(client, people, policy, make_invoice):
    invoice = make_invoice()
    tasks = engine.submit(invoice, people["clerk"])
    original = [(t.name, t.role, t.sla_hours, t.requires_signature) for t in tasks]
    login(client, people["admin"])
    response = client.post(reverse("workflow:policy_edit", args=[policy.pk]),
                           policy_data(policy.org_unit, policy))
    assert response.status_code == 302
    tasks = list(invoice.approval_tasks.order_by("order"))
    assert [(t.name, t.role, t.sla_hours, t.requires_signature) for t in tasks] == original
    assert all(t.step_id is None for t in tasks)
    engine.decide(tasks[0], people["verifier"], engine.Decision.APPROVE, "Checked source")
    another = make_invoice()
    new_tasks = engine.submit(another, people["clerk"])
    assert new_tasks[0].name == "Check documents"


def test_stale_policy_edit_is_rejected(client, people, policy):
    data = policy_data(policy.org_unit, policy)
    ApprovalPolicy.objects.filter(pk=policy.pk).update(name="Changed by another administrator")
    login(client, people["admin"])
    response = client.post(reverse("workflow:policy_edit", args=[policy.pk]), data)
    assert response.status_code == 200 and response.context["form"].non_field_errors()
    policy.refresh_from_db()
    assert policy.name == "Changed by another administrator" and policy.steps.count() == 3


def test_invoice_map_reports_current_step_deadline_and_missing_signing_setup(
        people, policy, make_invoice):
    invoice = make_invoice()
    tasks = engine.submit(invoice, people["clerk"])
    invoice.refresh_from_db()
    process = invoice_process(invoice, people["admin"])
    assert process["current"]["task"].pk == tasks[0].pk
    assert process["current"]["owners"] == [people["verifier"]]
    engine.decide(tasks[0], people["verifier"], engine.Decision.APPROVE, "Verified")
    process = invoice_process(invoice, people["admin"])
    assert process["current"]["signature"] and process["current"]["due_at"]
    assert process["current"]["state"] == "blocked" and process["needs_attention"]
    assert any("signing setup" in reason for reason in process["attention"])


def test_map_identifies_no_eligible_owner_and_overdue_without_mutating(
        people, policy, make_invoice, tree):
    invoice = make_invoice()
    tasks = engine.submit(invoice, people["clerk"])
    Membership.objects.filter(user=people["verifier"], role=Role.VERIFIER).delete()
    Membership.objects.create(user=people["clerk"], org_unit=tree["moh"], role=Role.VERIFIER)
    ApprovalTask.objects.filter(pk=tasks[0].pk).update(due_at=timezone.now() - timedelta(hours=1))
    invoice.refresh_from_db()
    before = AuditEvent.objects.count()
    process = invoice_process(invoice, people["admin"])
    assert process["current"]["state"] == "overdue"
    assert process["current"]["owner_count"] == 0
    assert any("separation-of-duties" in text for text in process["attention"])
    assert any("SLA" in text for text in process["attention"])
    assert AuditEvent.objects.count() == before
    tasks[0].refresh_from_db()
    assert tasks[0].status == ApprovalTask.Status.PENDING


def test_map_excludes_previous_decider_and_shows_signature_ready_staff(
        people, policy, make_invoice, tree, signers):
    invoice = make_invoice()
    tasks = engine.submit(invoice, people["clerk"])
    Membership.objects.create(user=people["verifier"], org_unit=tree["moh"], role=Role.APPROVER)
    engine.decide(tasks[0], people["verifier"], engine.Decision.APPROVE, "Verified")
    invoice.refresh_from_db()
    process = invoice_process(invoice, people["admin"])
    assert people["verifier"] not in process["current"]["owners"]
    assert process["current"]["signing_ready"] == 2 and process["current"]["state"] == "active"


def test_zero_amount_and_paid_legacy_maps_do_not_invent_payment_history(client, people, make_invoice):
    login(client, people["admin"])
    for amount, status in [("0", Invoice.Status.APPROVED), ("100", Invoice.Status.PAID)]:
        invoice = make_invoice(amount=amount)
        Invoice.objects.filter(pk=invoice.pk).update(status=status)
        invoice.refresh_from_db()
        process = invoice_process(invoice, people["admin"])
        assert process["terminal"] and not process["needs_attention"]
        assert not any("Authorise payment" == node["label"] for node in process["nodes"])
        assert process["current"]["state"] == "complete"
        assert client.get(reverse("workflow:process", args=[invoice.pk])).status_code == 200


def test_returned_invoice_previews_next_policy_and_retains_actual_round_history(people, policy, make_invoice):
    invoice = make_invoice()
    tasks = engine.submit(invoice, people["clerk"])
    engine.decide(tasks[0], people["verifier"], engine.Decision.RETURN, "Correct the billing period")
    ApprovalStep.objects.filter(policy=policy, order=1).update(name="New verification requirements")
    invoice.refresh_from_db()
    process = invoice_process(invoice, people["admin"])
    assert process["current"]["label"] == "Review / correction and submission"
    assert process["needs_attention"]
    assert any(n.get("preview") and n["label"] == "New verification requirements" for n in process["nodes"])
    assert not any(n.get("task") for n in process["nodes"])
    assert process["tasks"][0].comment == "Correct the billing period"
    engine.submit(invoice, people["clerk"])
    invoice.refresh_from_db()
    process = invoice_process(invoice, people["admin"])
    assert process["round"] == 2 and process["current"]["label"] == "New verification requirements"
    assert not any(n.get("preview") for n in process["nodes"])


def test_payment_authorisation_deadline_is_not_hidden_by_later_invoice_due_date(
        people, make_invoice, tree, signers):
    invoice = make_invoice()
    invoice.status = Invoice.Status.SCHEDULED
    instruction = SimpleNamespace(
        status="pending", org_unit=tree["moh"], prepared_by_id=people["finance"].pk,
        authorise_by=timezone.now() - timedelta(hours=1),
        get_absolute_url=lambda: "/settlements/1/", signings=MagicMock(),
    )
    instruction.signings.values_list.return_value = [people["finance"].pk]
    related = MagicMock()
    related.select_related.return_value.exclude.return_value.order_by.return_value.first.return_value = \
        SimpleNamespace(line=SimpleNamespace(instruction=instruction))
    with patch.object(Invoice, "settlement_items", new_callable=PropertyMock, return_value=related):
        process = invoice_process(invoice, people["admin"])
    assert process["current"]["label"] == "Authorise payment"
    assert process["current"]["state"] == "overdue"
    assert process["current"]["due_at"] == instruction.authorise_by
    assert any("payment authorisation deadline" in text for text in process["attention"])


def test_process_routes_and_filtered_summary_are_scoped(client, people, make_invoice, tree, policy):
    invoice = make_invoice()
    engine.submit(invoice, people["clerk"])
    login(client, people["outsider"])
    assert client.get(reverse("workflow:process", args=[invoice.pk])).status_code == 404
    listing = client.get(reverse("workflow:processes"))
    assert list(listing.context["processes"]) == []
    login(client, people["admin"])
    listing = client.get(reverse("workflow:processes"), {"q": invoice.reference, "status": "in_approval"})
    assert len(listing.context["processes"]) == 1
    assert sum(item["count"] for item in listing.context["summary"]) == 1
    response = client.get(reverse("workflow:process", args=[invoice.pk]))
    assert response.status_code == 200 and response.context["current"]["task"].name == "Verify"


def test_summary_counts_invoices_not_distinct_ordering_groups(client, local_admin, make_invoice, tree):
    for _ in range(3):
        make_invoice()
    login(client, local_admin)
    response = client.get(reverse("workflow:processes"), {"status": Invoice.Status.DRAFT})
    assert len(response.context["processes"]) == 3
    assert next(s["count"] for s in response.context["summary"] if s["key"] == "draft") == 3
    invalid = client.get(reverse("workflow:processes"), {"org_unit": tree["moe"].pk})
    assert invalid.context["invalid_filters"] and invalid.context["form"].errors["org_unit"]
    assert invalid.context["can_configure"] and "processes" not in invalid.context
