from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import ProcessingCompletion, ProcessingSchedule
from core.work_schedules import complete_cycle, following_date
from imports.models import ImportBatch, ImportRow
from org.models import Role
from tests.conftest import make_user
from tests.test_operations_control import login
from tests.test_operations_control import exception
from tests.test_bank_reconciliation import executed

pytestmark = pytest.mark.django_db


@pytest.fixture
def plan(people, tree):
    return ProcessingSchedule.objects.create(owner=people["clerk"], org_unit=tree["moh"],
        name="Weekly bill review", frequency="weekly", next_due_date=timezone.localdate() - timedelta(days=1),
        anchor_day=28)


@pytest.mark.parametrize("tab", ["tasks", "schedules", "anomalies", "overdue"])
@pytest.mark.parametrize("mode", ["dashboard", "list"])
def test_my_work_tabs_and_views_render_real_scoped_records(client, people, plan, make_invoice, tab, mode):
    invoice = make_invoice()
    invoice.flags = ["receipt_before_issue"]
    invoice.save()
    login(client, people["clerk"])
    response = client.get(reverse("core:my_work"), {"tab": tab, "mode": mode})
    assert response.status_code == 200
    assert b"My work" in response.content
    assert response.context["cards"]
    assert response.context["tab"] == tab
    if tab == "schedules":
        assert any(row["title"] == plan.name for row in response.context["rows"])
    if tab == "anomalies":
        assert any(row["title"] == invoice.reference for row in response.context["rows"])


def test_schedule_create_edit_pause_resume_and_no_financial_side_effect(client, people, tree, provider, account, make_invoice):
    invoice = make_invoice()
    original_status = invoice.status
    login(client, people["clerk"])
    data = {"name": "Monthly processing", "org_unit": tree["moh"].pk, "provider": provider.pk,
        "frequency": "monthly", "next_due_date": "2026-01-31", "is_active": "on", "notes": "Check received bills."}
    response = client.post(reverse("core:work_schedule_new"), data)
    assert response.status_code == 302
    plan = ProcessingSchedule.objects.get(owner=people["clerk"])
    assert plan.anchor_day == 31
    assert client.get(plan.get_absolute_url()).status_code == 200
    data["name"] = "Monthly review revised"
    assert client.post(plan.get_absolute_url(), data).status_code == 302
    action = reverse("core:work_schedule_action", args=[plan.pk])
    assert client.post(action, {"action": "toggle", "target_active": "0"}).status_code == 302
    plan.refresh_from_db()
    assert not plan.is_active
    assert client.post(action, {"action": "toggle", "target_active": "1"}).status_code == 302
    plan.refresh_from_db()
    assert plan.is_active
    invoice.refresh_from_db()
    assert invoice.status == original_status
    assert not invoice.paid_at


def test_completion_is_idempotent_and_preserves_missed_cycles(plan, people):
    due = plan.next_due_date
    schedule, created = complete_cycle(people["clerk"], plan.pk, due, "Bills checked")
    assert created
    assert schedule.next_due_date == due + timedelta(days=7)
    schedule, created = complete_cycle(people["clerk"], plan.pk, due)
    assert not created
    assert schedule.next_due_date == due + timedelta(days=7)
    assert ProcessingCompletion.objects.filter(schedule=plan).count() == 1
    plan.next_due_date = timezone.localdate() - timedelta(days=30)
    plan.save()
    schedule, _ = complete_cycle(people["clerk"], plan.pk, plan.next_due_date)
    assert schedule.next_due_date < timezone.localdate()  # Never silently discard missed weekly cycles.


def test_monthly_anchor_survives_short_and_leap_months(plan):
    plan.frequency, plan.next_due_date, plan.anchor_day = "monthly", date(2024, 1, 31), 31
    assert following_date(plan) == date(2024, 2, 29)
    plan.next_due_date = date(2024, 2, 29)
    assert following_date(plan) == date(2024, 3, 31)
    plan.next_due_date = date(2026, 12, 31)
    assert following_date(plan) == date(2027, 1, 31)


def test_completed_once_plan_requires_new_date_before_resume(client, plan, people):
    plan.frequency = "once"
    plan.save()
    complete_cycle(people["clerk"], plan.pk, plan.next_due_date)
    login(client, people["clerk"])
    client.post(reverse("core:work_schedule_action", args=[plan.pk]), {"action": "toggle", "target_active": "1"})
    plan.refresh_from_db()
    assert not plan.is_active
    assert plan.completions.count() == 1


def test_schedule_ownership_and_ministry_boundaries(client, plan, people, tree):
    same_unit = make_user("other-personal-plan-owner", [(Role.CLERK, tree["moh"])])
    login(client, same_unit)
    assert client.get(plan.get_absolute_url()).status_code == 404
    assert client.post(reverse("core:work_schedule_action", args=[plan.pk]),
        {"action": "complete", "due_token": plan.next_due_date.isoformat()}).status_code == 404
    response = client.get(reverse("core:my_work"), {"tab": "schedules", "mode": "list"})
    assert response.context["total_count"] == 0
    login(client, people["clerk"])
    foreign = client.post(reverse("core:work_schedule_new"), {"name": "Forbidden",
        "org_unit": tree["moe"].pk, "frequency": "weekly", "next_due_date": "2026-12-01", "is_active": "on"})
    assert foreign.status_code == 200
    assert "org_unit" in foreign.context["form"].errors


def test_submitted_anomalies_do_not_include_other_uploaders(client, people, provider):
    own = ImportBatch.objects.create(uploaded_by=people["clerk"], provider=provider, kind="spreadsheet",
        original_name="own.csv", status="staged", sha256="a"*64)
    theirs = ImportBatch.objects.create(uploaded_by=people["finance"], provider=provider, kind="spreadsheet",
        original_name="private.csv", status="staged", sha256="b"*64)
    ImportRow.objects.create(batch=own, row_number=1, status="invalid", errors=["Invalid invoice amount"])
    ImportRow.objects.create(batch=theirs, row_number=1, status="invalid", errors=["Private unrelated problem"])
    login(client, people["clerk"])
    response = client.get(reverse("core:my_work"), {"tab": "anomalies", "mode": "list", "category": "import_anomalies"})
    assert response.context["total_count"] == 1
    assert b"Invalid invoice amount" in response.content
    assert b"Private unrelated problem" not in response.content


def test_all_schedule_listing_pages_are_accessible_and_private(client, people, tree):
    for index in range(35):
        ProcessingSchedule.objects.create(owner=people["clerk"], org_unit=tree["moh"],
            name=f"Plan {index:02d}", next_due_date=timezone.localdate())
    login(client, people["clerk"])
    url = reverse("core:my_work")
    first = client.get(url, {"tab": "schedules", "mode": "list"})
    second = client.get(url, {"tab": "schedules", "mode": "list", "page": "2"})
    assert first.context["total_count"] == 35
    assert len(first.context["rows"]) == 30 and len(second.context["rows"]) == 5
    assert {row["title"] for row in first.context["rows"]}.isdisjoint(
        {row["title"] for row in second.context["rows"]})


@pytest.mark.parametrize("target", ["core:dashboard", "core:control_centre", "core:my_work", "providers:accounts", "providers:visibility"])
def test_overdue_panel_shared_across_dashboards(client, people, plan, make_invoice, target):
    invoice = make_invoice()
    invoice.due_date = timezone.localdate() - timedelta(days=2)
    invoice.save()
    login(client, people["clerk"])
    response = client.get(reverse(target))
    assert response.status_code == 200
    panel = response.context["dashboard_overdue"]
    assert panel
    cards = {card["label"]: card for card in panel["cards"]}
    assert cards["Overdue invoices"]["count"] == 1
    assert cards["Processing schedules"]["count"] == 1
    drill = client.get(cards["Overdue invoices"]["url"])
    assert drill.status_code == 200
    assert drill.context["total_count"] == 1


def test_pause_does_not_hide_overdue_bill_or_expose_other_persons_plan(client, people, plan, tree, make_invoice):
    ProcessingSchedule.objects.create(owner=people["finance"], org_unit=tree["moh"],
        name="Other person's private plan", next_due_date=timezone.localdate()-timedelta(days=5))
    invoice = make_invoice()
    invoice.due_date = timezone.localdate()-timedelta(days=1)
    invoice.save()
    login(client, people["clerk"])
    client.post(reverse("core:work_schedule_action", args=[plan.pk]), {"action": "toggle", "target_active": "0"})
    response = client.get(reverse("core:my_work"), {"tab": "overdue", "mode": "list"})
    assert any(row["title"] == invoice.reference for row in response.context["rows"])
    assert all(row["title"] != "Other person's private plan" for row in response.context["rows"])
    assert not any(row["title"] == plan.name for row in response.context["rows"])


def test_bank_and_settlement_overdue_are_visible_only_to_authorised_roles(client, exception, executed, people):
    exception.resolution_due_date = timezone.localdate() - timedelta(days=1)
    exception.assigned_to = people["finance"]
    exception.save()
    executed.status, executed.executed_at = "draft", None
    executed.pay_by = timezone.localdate() - timedelta(days=1)
    executed.save()
    login(client, people["finance"])
    response = client.get(reverse("core:my_work"), {"tab": "overdue", "mode": "list"})
    counts = {card["key"]: card["count"] for card in response.context["cards"]}
    assert counts["bank"] == 1 and counts["settlement"] == 1
    login(client, people["clerk"])
    response = client.get(reverse("core:my_work"), {"tab": "overdue", "mode": "list"})
    counts = {card["key"]: card["count"] for card in response.context["cards"]}
    assert counts["bank"] == 0 and counts["settlement"] == 0


def test_assigned_approvals_are_batch_based_and_unbatched_overdue_work_is_not_hidden(
        client, people, tree, make_invoice):
    from operations.services import create_batch
    from workflow.models import ApprovalTask
    first, second, unbatched = make_invoice(), make_invoice(), make_invoice()
    batch = create_batch(people["clerk"], tree["moh"], "Two-invoice approval", [first.pk, second.pk])
    for invoice in [first, second, unbatched]:
        invoice.status = "in_approval"
        invoice.save()
        ApprovalTask.objects.create(invoice=invoice, order=1, name="Verifier",
            role=Role.VERIFIER, escalation_role=Role.APPROVER, sla_hours=24,
            status="pending", activated_at=timezone.now()-timedelta(days=2),
            due_at=timezone.now()-timedelta(days=1))
    login(client, people["verifier"])
    response = client.get(reverse("core:my_work"), {"tab": "tasks", "mode": "list", "category": "approval"})
    assert response.context["total_count"] == 1
    assert response.context["rows"][0]["title"] == batch.reference
    response = client.get(reverse("core:my_work"), {"tab": "overdue", "mode": "list", "category": "approval_exceptions"})
    assert response.context["total_count"] == 1
    assert response.context["rows"][0]["title"] == unbatched.reference
    assert "do not approve individually" in response.context["rows"][0]["next_action"]


def test_paid_invoice_findings_are_historical_not_overdue(client, people, make_invoice):
    invoice = make_invoice()
    invoice.status, invoice.flags = "paid", ["late_receipt"]
    invoice.due_date = timezone.localdate()-timedelta(days=10)
    invoice.save()
    login(client, people["clerk"])
    response = client.get(reverse("core:my_work"), {"tab": "anomalies", "mode": "list", "category": "invoice_anomalies"})
    assert response.context["total_count"] == 1
    assert not response.context["rows"][0]["overdue"]


def test_provider_mismatch_assigned_to_clerk_is_not_lost_from_my_work(client, people, tree, provider, make_invoice):
    from reconciliation.models import Statement, StatementRow
    invoice = make_invoice()
    statement = Statement.objects.create(provider=provider, org_unit=tree["moh"], currency="MUR",
        statement_date=timezone.localdate(), uploaded_by=people["finance"])
    row = StatementRow.objects.create(statement=statement, line_number=1, assigned_unit=tree["moh"],
        assigned_to=people["clerk"], invoice=invoice, amount=999, status="unsorted", reason="Amount differs")
    login(client, people["clerk"])
    response = client.get(reverse("core:my_work"), {"tab": "tasks", "mode": "list", "category": "provider"})
    assert response.status_code == 200
    assert response.context["total_count"] == 1
    assert response.context["rows"][0]["url"] == row.get_absolute_url()


def test_authorisation_deadline_is_late_even_when_payment_date_is_future(client, executed, people):
    executed.status, executed.executed_at = "pending", None
    executed.pay_by = timezone.localdate()+timedelta(days=7)
    executed.authorise_by = timezone.now()-timedelta(hours=1)
    executed.save()
    login(client, people["authoriser"])
    response = client.get(reverse("core:my_work"), {"tab": "tasks", "mode": "list", "category": "settlement"})
    assert response.context["total_count"] == 1
    assert response.context["rows"][0]["overdue"]
    assert "authorisation deadline" in response.context["rows"][0]["detail"]
    assert next(card for card in response.context["cards"] if card["key"] == "settlement")["overdue"] == 1


def test_returned_processing_deadline_is_late_even_when_bill_due_date_is_future(client, people, make_invoice):
    invoice = make_invoice()
    invoice.status = "returned"
    invoice.must_approve_by = timezone.now()-timedelta(hours=1)
    invoice.save()
    login(client, people["clerk"])
    for tab, category in [("tasks", "preparation"), ("anomalies", "invoice_anomalies")]:
        response = client.get(reverse("core:my_work"), {"tab": tab, "mode": "list", "category": category})
        assert response.context["rows"][0]["overdue"]
        assert next(card for card in response.context["cards"] if card["key"] == category)["overdue"] == 1


def test_detected_uncommitted_files_follow_unit_and_provider_filters(client, people, tree, provider, account):
    batch = ImportBatch.objects.create(uploaded_by=people["clerk"], provider=None, kind="spreadsheet",
        original_name="auto-detected.csv", status="staged", sha256="c"*64)
    ImportRow.objects.create(batch=batch, row_number=1, status="invalid", errors=["Amount needs checking"],
        data={"service_account_id": account.pk, "provider_id": provider.pk})
    login(client, people["clerk"])
    for tab, category in [("tasks", "intake"), ("anomalies", "import_anomalies")]:
        response = client.get(reverse("core:my_work"), {"tab": tab, "mode": "list", "category": category,
            "org_unit": tree["moh"].pk, "provider": provider.pk})
        assert response.status_code == 200
        assert response.context["total_count"] == 1
