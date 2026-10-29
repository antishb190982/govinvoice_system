from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.models import Notification
from invoices.models import Invoice
from tests.test_operations_control import login

pytestmark = pytest.mark.django_db


def bill(account, provider, user, number="TIMING-1", currency="MUR", surcharge="10"):
    today = timezone.localdate()
    return Invoice.objects.create(
        reference="TEST-" + number,
        service_account=account, provider=provider, org_unit=account.org_unit,
        utility_type=provider.utility_type, invoice_number=number,
        issue_date=today - timedelta(days=30), received_date=today - timedelta(days=1),
        due_date=today - timedelta(days=2), amount_total=Decimal("100"),
        surcharge_amount=Decimal(surcharge), currency=currency, created_by=user,
        submitted_at=timezone.now(),
    )


def test_bell_contains_owned_alerts_live_overdue_and_no_repeated_banner(client, people, account, provider):
    user = people["clerk"]
    invoice = bill(account, provider, user)
    Notification.objects.create(user=user, title="Own alert", body="Check the evidence", link=invoice.get_absolute_url())
    other = User.objects.create_user(username="separate-alert-owner")
    Notification.objects.create(user=other, title="Private foreign alert")
    login(client, user)
    response = client.get(reverse("core:my_space"))
    assert response.status_code == 200
    assert b"attention-menu" in response.content
    assert b"Own alert" in response.content
    assert b"Private foreign alert" not in response.content
    assert b'<section class="card mw-overdue"' not in response.content
    payload = client.get(reverse("core:activity")).json()
    assert payload["attention"]["unread_count"] == 1
    assert payload["attention"]["overdue_count"] >= 1
    assert invoice.reference in str(payload["attention"]["overdue"])


def test_alert_links_cannot_inject_an_external_or_script_url(client, people):
    user = people["clerk"]
    Notification.objects.create(user=user, title="Safe link", link="javascript:alert(1)")
    login(client, user)
    payload = client.get(reverse("core:activity")).json()
    assert payload["attention"]["alerts"][0]["url"] == reverse("core:notifications")


def test_ministry_report_counts_lateness_once_separates_currency_and_exports(client, people, account, provider):
    user = people["clerk"]
    first = bill(account, provider, user)
    bill(account, provider, user, "TIMING-2")
    bill(account, provider, user, "TIMING-USD", currency="USD", surcharge="99")
    login(client, user)
    response = client.get(reverse("core:ministry_timeliness"))
    assert response.status_code == 200
    totals = response.context["totals"]
    assert totals["invoices"] == 2
    assert totals["late"] == 2  # Both receipt and submission late: still one invoice each.
    assert totals["repeat"] == 1
    assert totals["late_surcharge"] == Decimal("20")
    assert response.content.count(b'name="provider"') == 1
    assert b'name="utility_provider"' not in response.content
    summary = client.get(reverse("core:ministry_timeliness"), {"export": "summary"})
    assert summary["Content-Type"].startswith("text/csv")
    assert b"Ministry (authorised records only)" in summary.content
    evidence = client.get(reverse("core:ministry_timeliness"), {"export": "invoices"})
    assert first.reference.encode() in evidence.content
    assert b"TIMING-USD" not in evidence.content
    detail = client.get(reverse("core:ministry_timeliness"), {"tab": "invoices"})
    assert detail.status_code == 200
    assert first.reference.encode() in detail.content


def test_report_validates_filters_and_never_exposes_unassigned_users_data(client, account, provider, people):
    invoice = bill(account, provider, people["clerk"])
    unrelated = User.objects.create_user(username="unassigned-report-user")
    login(client, unrelated)
    response = client.get(reverse("core:ministry_timeliness"))
    assert response.status_code == 200
    assert response.context["totals"]["invoices"] == 0
    assert invoice.reference.encode() not in response.content
    invalid = client.get(reverse("core:ministry_timeliness"), {"start": "2026-12-01", "end": "2026-01-01"})
    assert invalid.status_code == 400
