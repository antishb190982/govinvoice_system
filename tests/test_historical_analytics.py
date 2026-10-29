import csv
import io
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import OTP_SESSION_KEY
from analytics import queries
from analytics.history import historical_dashboard
from invoices.models import Invoice
from providers.models import Provider, ServiceAccount, UtilityType
from workflow.models import ApprovalTask

pytestmark = pytest.mark.django_db


@pytest.fixture
def end():
    return queries.last_complete_month()


@pytest.fixture
def billed(make_invoice):
    def create(month, amount="100.000", status=Invoice.Status.APPROVED, account=None, **extra):
        inv = make_invoice(amount=amount)
        fields = {"issue_date": queries.add_months(month, -1).replace(day=5),
                  "period_end": month.replace(day=28), "status": status, **extra}
        if account:
            fields.update(service_account=account, provider=account.provider, org_unit=account.org_unit,
                          utility_type=account.provider.utility_type)
        Invoice.objects.filter(pk=inv.pk).update(**fields)
        inv.refresh_from_db()
        return inv
    return create


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def test_historical_cohort_totals_statuses_and_equal_period_comparison(billed, end):
    for offset in (1, 2, 3):
        billed(queries.add_months(end, -offset))
    billed(end, "200.000", Invoice.Status.PAID, paid_at=end.replace(day=15))
    billed(queries.add_months(end, -8), "250.000")
    billed(queries.add_months(end, -12), "9000.000")
    billed(queries.add_months(end, 1), "9000.000")
    billed(end, "1000.000", Invoice.Status.DRAFT)
    billed(end, "2000.000", Invoice.Status.CANCELLED)
    history = historical_dashboard(Invoice.objects.all(), 6, end)
    total = history["totals"]
    assert total["total"] == Decimal("500.000") and total["count"] == 4
    assert total["paid"] == Decimal("200.000") and total["outstanding"] == Decimal("300.000")
    assert total["average"] == Decimal("125.000")
    assert total["previous"] == Decimal("250.000") and total["change_pct"] == 100
    assert history["drafts_excluded"] == 1
    assert len(history["ledger"]) == 6 and history["ledger"][-1]["total"] == 200
    assert history["providers"][0]["share"] == 100
    insight = history["insights"][0]
    assert insight["kind"] == "rise" and insight["baseline"] == 100 and insight["change_pct"] == 100


def test_missing_month_is_a_gap_not_a_confirmed_cost_reduction(billed, end):
    for offset in (1, 2, 3):
        billed(queries.add_months(end, -offset))
    history = historical_dashboard(Invoice.objects.all(), 6, end)
    assert history["ledger"][-1]["change_pct"] is None
    insight = history["insights"][0]
    assert insight["kind"] == "gap" and insight["latest"] is None and insight["change_pct"] is None


def test_no_baseline_does_not_produce_an_invented_percentage(billed, end):
    billed(end)
    history = historical_dashboard(Invoice.objects.all(), 6, end)
    assert history["insights"] == []
    assert history["totals"]["change_pct"] is None
    assert history["providers"][0]["change_pct"] is None


def test_completely_missing_current_period_is_not_minus_100_percent(billed, end):
    billed(queries.add_months(end, -8))
    history = historical_dashboard(Invoice.objects.all(), 6, end)
    assert history["totals"]["count"] == 0 and history["totals"]["change_pct"] is None
    assert history["providers"][0]["count"] == 0 and history["providers"][0]["change_pct"] is None
    assert history["totals"]["provider_count"] == 0


def test_issue_date_is_used_only_when_billing_period_is_missing(billed, end):
    billed(end, "10.000", issue_date=queries.add_months(end, -8))
    billed(end, "20.000", period_end=None, issue_date=end.replace(day=7))
    history = historical_dashboard(Invoice.objects.all(), 6, end)
    assert history["totals"]["total"] == 30 and history["ledger"][-1]["count"] == 2


def test_provider_chart_other_bucket_preserves_entire_total(billed, provider, tree, end):
    for i in range(10):
        p = Provider.objects.create(name=f"Provider {i}", short_code=f"PV{i}",
                                    utility_type=UtilityType.ELECTRICITY, currency="MUR")
        account = ServiceAccount.objects.create(provider=p, org_unit=tree["hosp"], account_number=f"ACC-{i}")
        billed(end, str(i + 1), account=account)
    history = historical_dashboard(Invoice.objects.all(), 6, end)
    assert len(history["providers"]) == 10 and history["totals"]["total"] == 55
    assert len(history["provider_trend"]["series"]) == 7
    assert sum(s["data"][-1] for s in history["provider_trend"]["series"]) == 55
    assert history["provider_chart"]["labels"][-1] == "Other providers"
    assert sum(history["provider_chart"]["series"][0]["data"]) == 55


def test_finance_and_admin_scope_currency_and_provider_filters(
        client, billed, end, people, provider, tree):
    billed(end)
    outside = ServiceAccount.objects.create(provider=provider, org_unit=tree["moe"], account_number="OUTSIDE")
    billed(end, "900.000", account=outside)
    billed(end, "5000.000", currency="USD")
    login(client, people["finance"])
    response = client.get(reverse("analytics"), {"months": 6, "end_month": end.strftime("%Y-%m")})
    assert response.status_code == 200 and response.context["history"]["totals"]["total"] == 100
    assert sum(response.context["monthly"]["series"][0]["data"]) == 100
    exported = client.get(reverse("analytics"), {"months": 6, "export": "providers"})
    records = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert Decimal(records[1][4]) == 100
    login(client, people["admin"])
    response = client.get(reverse("analytics"), {"months": 6})
    assert response.context["history"]["totals"]["total"] == 1000
    response = client.get(reverse("analytics"), {"months": 6, "org_unit": tree["moh"].pk})
    assert response.context["history"]["totals"]["total"] == 100


def test_invalid_or_unauthorised_filters_show_errors_without_fallback_data(client, people, tree, end):
    login(client, people["finance"])
    for params in ({"org_unit": tree["moe"].pk}, {"months": 999},
                   {"end_month": queries.add_months(end, 1).strftime("%Y-%m")}, {"currency": "MURR"}):
        response = client.get(reverse("analytics"), params)
        assert response.status_code == 200 and response.context["invalid_filters"]
        assert "history" not in response.context
        response = client.get(reverse("analytics"), {**params, "export": "csv"})
        assert response.status_code == 400


def test_exports_keep_decimal_precision_and_prevent_spreadsheet_formulas(
        client, billed, end, people, provider):
    billed(end, "0.100")
    billed(end, "0.200")
    provider.name = "=SUM(1,2)"
    provider.save(update_fields=["name"])
    login(client, people["finance"])
    monthly = client.get(reverse("analytics"), {"months": 6, "export": "csv"})
    records = list(csv.reader(io.StringIO(monthly.content.decode("utf-8-sig"))))
    assert Decimal(records[-1][4]) == Decimal("0.300")
    assert records[-1][4] == "0.300"
    providers = client.get(reverse("analytics"), {"months": 6, "export": "providers"})
    records = list(csv.reader(io.StringIO(providers.content.decode("utf-8-sig"))))
    assert records[1][0] == "'=SUM(1,2)"


def test_provider_and_dates_filter_processing_metrics_too(client, billed, end, people, tree, provider):
    other_provider = Provider.objects.create(name="Water", short_code="WTR", utility_type=UtilityType.WATER)
    other_account = ServiceAccount.objects.create(provider=other_provider, org_unit=tree["hosp"],
                                                 account_number="WTR-1")
    invoices = [billed(end), billed(end, account=other_account)]
    when = timezone.make_aware(datetime.combine(end.replace(day=15), time(10)))
    for inv in invoices:
        ApprovalTask.objects.create(invoice=inv, round=1, order=1, name="Review", role="approver",
                                    sla_hours=8, status="approved", activated_at=when - timedelta(hours=1),
                                    due_at=when + timedelta(hours=1), decided_at=when)
    login(client, people["finance"])
    response = client.get(reverse("analytics"), {"provider": provider.pk, "months": 6})
    assert response.context["sla"]["decided"] == 1
    assert response.context["history"]["totals"]["total"] == 100


def test_consumption_keeps_recorded_zeroes_missing_months_and_units_separate(billed, end):
    billed(queries.add_months(end, -1), consumption="0", consumption_unit="kWh")
    billed(end, consumption="30", consumption_unit="kWh")
    billed(end, consumption="1", consumption_unit="MWh")
    trend = queries.consumption_trend(Invoice.objects.all(), 6, end=end)
    kwh = next(s for s in trend["series"] if s["label"].endswith("(kWh)"))
    assert kwh["data"][-2:] == [0, 200]
    assert kwh["data"][0] is None and kwh["average"] == 15
    assert len(trend["series"]) == 2


def test_projection_uses_selected_history_not_future_records_and_requires_observations(billed, end):
    for offset in (0, 1, 2):
        billed(queries.add_months(end, -offset))
    billed(queries.add_months(end, 1), "9000.000")
    projection = queries.forecast_next_month(Invoice.objects.all(), end=end)
    assert projection["month"] == queries.add_months(end, 1)
    assert projection["rows"][0]["basis"] == 100
    older = queries.forecast_next_month(Invoice.objects.all(), end=queries.add_months(end, -1))
    assert older["rows"] == []  # two observed months, not a fabricated three-month baseline


def test_blank_default_filters_render_the_same_selection_the_server_used(client, people, end):
    login(client, people["finance"])
    response = client.get(reverse("analytics"), {"months": "", "currency": "", "end_month": ""})
    assert response.context["months"] == 12
    assert response.context["form"]["months"].value() == "12"
    assert response.context["form"]["currency"].value() == response.context["currency"]
    assert response.context["form"]["end_month"].value() == end.strftime("%Y-%m")
