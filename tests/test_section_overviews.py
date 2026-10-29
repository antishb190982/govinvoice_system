from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from analytics.sections import SECTIONS, records_url
from invoices.models import Invoice
from tests.conftest import make_user
from tests.test_views import sign_in

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("section", list(SECTIONS))
def test_overview_and_its_drill_links_render(client, people, make_invoice, section):
    make_invoice()
    root = make_user("overview-root", superuser=True)
    sign_in(client, root)
    response = client.get(reverse("core:section_overview", args=[section]))
    assert response.status_code == 200
    assert response.context["section_tabs"]["active"] == "overview"
    assert b"Overview (dashboard)" in response.content
    for item in response.context["cards"] + response.context["breakdown"] + response.context["providers"] + response.context["amounts"]:
        assert item["url"].startswith("/")
        result = client.get(item["url"])
        assert result.status_code == 200, (section, item, result.status_code)


def test_invoice_drilldown_counts_and_currency_are_exact(client, people, make_invoice):
    first = make_invoice()
    first.status, first.currency = Invoice.Status.APPROVED, "MUR"
    first.due_date = timezone.localdate() - timedelta(days=1)
    first.save()
    second = make_invoice()
    second.status, second.currency = Invoice.Status.PAID, "USD"
    second.save()
    make_invoice()  # Draft, excluded from billed totals but included in records.
    sign_in(client, people["clerk"])
    response = client.get(reverse("core:section_overview", args=["invoices"]))
    assert response.context["cards"][0]["value"] == 3
    amounts = response.context["amounts"]
    assert {r["currency"] for r in amounts} == {"MUR", "USD"}
    for row in amounts:
        result = client.get(row["url"])
        assert result.context["form"].is_valid()
        assert result.context["page"].paginator.count == row["count"] == 1
        assert all(i.currency == row["currency"] for i in result.context["page"])
    result = client.get(response.context["cards"][2]["url"])
    assert result.context["page"].paginator.count == 1


def test_outsider_overview_cannot_see_other_ministry(client, people, make_invoice):
    make_invoice()
    sign_in(client, people["outsider"])
    result = client.get(reverse("core:section_overview", args=["invoices"]))
    assert result.context["cards"][0]["value"] == 0
    assert not result.context["providers"] and not result.context["amounts"]


def test_overview_respects_bank_and_import_permissions(client, people):
    sign_in(client, people["verifier"])
    for section in ("bank", "imports"):
        assert client.get(reverse("core:section_overview", args=[section])).status_code == 403


def test_pending_approval_drilldown_uses_same_user_and_attention_filter(client, people, policy, make_invoice):
    from workflow import engine
    from operations.services import create_batch, start_batch
    from tests.test_reconciliation import upload
    from reconciliation.services import assign, recheck

    invoice = make_invoice(reconciled=False)
    batch = create_batch(people["clerk"], invoice.org_unit.parent, "Overdue approval batch", [invoice.pk])
    statement = upload(people["finance"], invoice)
    row = statement.rows.get()
    assign(row, people["finance"], invoice.org_unit, people["clerk"], "Test matching statement line")
    recheck(row, people["finance"])
    start_batch(batch, people["clerk"])
    task = invoice.approval_tasks.get(status="pending")
    task.due_at = timezone.now() - timedelta(minutes=5)
    task.save()
    sign_in(client, people["verifier"])
    result = client.get(reverse("core:section_overview", args=["approvals"]))
    assert result.context["cards"][0]["value"] == 1
    overdue = result.context["cards"][1]
    assert overdue["value"] == 1
    result = client.get(overdue["url"])
    assert [row["batch"].pk for row in result.context["batch_rows"]] == [batch.pk]


def test_tabs_on_records_not_details_and_unknown_section(client, people, make_invoice):
    invoice = make_invoice()
    sign_in(client, people["clerk"])
    result = client.get(reverse("invoices:list"))
    assert result.context["section_tabs"]["active"] == "records"
    assert "section_tabs" not in client.get(invoice.get_absolute_url()).context
    assert client.get(reverse("core:section_overview", args=["unknown"])).status_code == 404


def test_import_status_drilldown_private_to_uploader(client, people):
    from imports.models import ImportBatch

    ImportBatch.objects.create(uploaded_by=people["clerk"], original_name="mine.csv", status="staged")
    ImportBatch.objects.create(uploaded_by=people["outsider"], original_name="private.csv", status="staged")
    sign_in(client, people["clerk"])
    result = client.get(reverse("core:section_overview", args=["imports"]))
    assert result.context["cards"][0]["value"] == 1
    assert client.get(records_url("imports", status="committed")).status_code == 200
