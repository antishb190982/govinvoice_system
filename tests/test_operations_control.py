from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import OTP_SESSION_KEY
from core.models import AuditEvent, Notification
from org.models import Role
from reconciliation.cases import update_case
from reconciliation.models import BankRow, BankStatement
from tests.conftest import make_user
from tests.test_bank_reconciliation import executed

pytestmark = pytest.mark.django_db


def test_unmatched_routes_and_favicon_do_not_crash_shared_navigation(client, people):
    assert client.get("/not-a-real-operational-page/").status_code == 404
    login(client, people["finance"])
    assert client.get("/not-a-real-operational-page/").status_code == 404
    icon = client.get("/favicon.ico")
    assert icon.status_code == 302
    assert "favicon" in icon.url


@pytest.fixture
def exception(people, tree, executed):
    statement = BankStatement.objects.create(org_unit=tree["moh"], account_label="TEST-OPERATIONS",
        currency="MUR", statement_date=timezone.localdate(), uploaded_by=people["finance"],
        filename="test.csv", sha256="b" * 64)
    return BankRow.objects.create(statement=statement, line_number=1, amount=Decimal("1001"),
        instruction=executed, settlement_reference=executed.reference, reason="Amount differs",
        status="exception")


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def test_case_owner_deadline_audit_and_notification_preserve_financial_state(exception, people, executed):
    due = timezone.localdate() + timedelta(days=2)
    updated = update_case(exception, people["finance"], people["finance"], due, "Confirm amount with bank.")
    assert updated.assigned_to == people["finance"]
    assert updated.resolution_due_date == due
    assert updated.status == "exception"
    executed.refresh_from_db()
    assert executed.status == "executed"
    assert executed.total_amount == Decimal("1000")
    event = AuditEvent.objects.get(action="bank.case_updated")
    assert event.data["owner"] == people["finance"].pk
    assert event.org_unit_id == exception.statement.org_unit_id
    assert Notification.objects.filter(user=people["finance"], link=exception.get_absolute_url()).exists()


def test_case_rejects_foreign_owner_and_reader_edits(exception, people, tree):
    foreign = make_user("foreign-case-finance", [(Role.FINANCE, tree["moe"])])
    due = timezone.localdate() + timedelta(days=1)
    with pytest.raises(ValidationError):
        update_case(exception, people["finance"], foreign, due, "Wrong unit")
    with pytest.raises(PermissionDenied):
        update_case(exception, people["authoriser"], people["finance"], due, "Reader cannot assign")
    assert not AuditEvent.objects.filter(action="bank.case_updated").exists()


def test_matched_row_cannot_be_marked_as_open_by_assignment(exception, people):
    exception.status = "matched"
    exception.save(update_fields=["status"])
    with pytest.raises(ValidationError):
        update_case(exception, people["finance"], people["finance"], timezone.localdate(), "Do not reopen.")


def test_assignment_endpoint_validates_and_persists_history(client, exception, people):
    login(client, people["finance"])
    response = client.post(exception.get_absolute_url(), {"action": "assign_case",
        "assigned_to": people["finance"].pk, "resolution_due_date": timezone.localdate().isoformat(),
        "note": "Request bank evidence."})
    assert response.status_code == 302
    response = client.get(exception.get_absolute_url())
    assert response.status_code == 200
    assert b"Request bank evidence." in response.content
    assert b"Exception ownership" in response.content
    exception.refresh_from_db()
    assert exception.assigned_to_id == people["finance"].pk
    response = client.post(exception.get_absolute_url(), {"action": "assign_case",
        "assigned_to": people["finance"].pk, "resolution_due_date": "2001-01-01", "note": "Invalid deadline"})
    assert response.status_code == 200
    assert "resolution_due_date" in response.context["case_form"].errors
    assert AuditEvent.objects.filter(action="bank.case_updated").count() == 1


def test_bank_readers_and_other_units_cannot_assign(client, exception, people, tree):
    login(client, people["authoriser"])
    response = client.get(exception.get_absolute_url())
    assert response.status_code == 200
    assert response.context["case_form"] is None
    assert client.post(exception.get_absolute_url(), {"action": "assign_case"}).status_code == 403
    foreign = make_user("foreign-case-viewer", [(Role.FINANCE, tree["moe"])])
    login(client, foreign)
    assert client.get(exception.get_absolute_url()).status_code == 404


def test_dashboard_uses_scoped_counts_and_exact_drill_filters(client, exception, people, tree, provider, account):
    login(client, people["finance"])
    url = reverse("core:control_centre")
    response = client.get(url)
    assert response.status_code == 200
    card = next(card for card in response.context["cards"] if card["key"] == "bank")
    assert card["count"] == 1 and card["unassigned"] == 1
    response = client.get(card["url"])
    assert response.context["total_count"] == 1
    assert response.context["rows"][0]["url"] == exception.get_absolute_url()
    filtered = client.get(url, {"queue": "bank", "provider": provider.pk, "assignment": "unassigned"})
    assert filtered.status_code == 200
    assert filtered.context["total_count"] == 1
    assert all("provider=" in card["url"] and "assignment=unassigned" in card["url"]
        for card in filtered.context["cards"])
    canonical = client.get(url, {"queue": "bank"})
    assert canonical.status_code == 200
    assert canonical.context["utility_provider"] is None
    cleared = client.get(url, {"provider": ""}, follow=True)
    assert cleared.status_code == 200
    assert not client.session.get("utility_provider")
    foreign = make_user("foreign-control-finance", [(Role.FINANCE, tree["moe"])])
    login(client, foreign)
    response = client.get(url, {"queue": "bank"})
    assert response.context["total_count"] == 0
    assert client.get(url, {"org_unit": tree["moh"].pk}).status_code == 400


def test_dashboard_ownership_overdue_and_inactive_filters(client, exception, people):
    exception.assigned_to = people["finance"]
    exception.resolution_due_date = timezone.localdate() - timedelta(days=1)
    exception.save()
    login(client, people["finance"])
    url = reverse("core:control_centre")
    response = client.get(url, {"queue": "bank", "assignment": "mine"})
    assert response.context["total_count"] == 1
    assert response.context["rows"][0]["overdue"] is True
    response = client.get(url, {"queue": "bank", "assignment": "unassigned"})
    assert response.context["total_count"] == 0
    # Recording progress may retain an overdue deadline rather than silently extending it.
    update_case(exception, people["finance"], people["finance"], exception.resolution_due_date, "Awaiting bank response.")
    response = client.get(url, {"queue": "bank"})
    assert response.context["rows"][0]["overdue"] is True
