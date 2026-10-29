import pytest
from django.urls import reverse

from tests.test_operations_control import login

pytestmark = pytest.mark.django_db


def test_provider_selection_is_explicit_and_does_not_follow_into_other_pages(client, people, provider, account):
    login(client, people["clerk"])
    selected = client.get(reverse("core:my_space"), {"provider": provider.pk})
    assert selected.context["utility_provider"].pk == provider.pk
    fresh = client.get(reverse("core:my_space"))
    assert fresh.status_code == 200
    assert fresh.context["utility_provider"] is None
    assert "utility_provider" not in client.session


def test_personal_workspace_has_one_navigation_and_one_canonical_work_view(client, people):
    login(client, people["clerk"])
    response = client.get(reverse("core:my_space"))
    tabs = response.context["personal_space_tabs"]
    work = next(tab for tab in tabs if tab["key"] == "work")
    assert reverse("core:my_work") in work["url"] and "mode=dashboard" in work["url"]
    assert response.content.count(b"<span>My space</span>") == 1
    assert b"<span>My work</span>" not in response.content
    assert b"<span>Control centre</span>" not in response.content
    assert b"<span>All batches register</span>" not in response.content
    assert reverse("core:section_overview", args=["invoices"]).encode() in response.content


def test_dashboard_record_tabs_preserve_explicit_provider(client, people, provider, account):
    login(client, people["clerk"])
    response = client.get(reverse("core:section_overview", args=["invoices"]), {"provider": provider.pk})
    nav = response.context["section_tabs"]
    assert nav["active"] == "overview"
    assert f"provider={provider.pk}" in nav["records_url"]
    target = client.get(nav["records_url"])
    assert target.context["utility_provider"].pk == provider.pk


def test_org_landing_defaults_to_real_dashboard_and_preserves_register(client, people, account):
    login(client, people["clerk"])
    overview = client.get(reverse("org:tree"))
    assert overview.status_code == 200
    assert overview.context["org_view"] == "overview"
    assert len(overview.context["org_dashboard_cards"]) == 4
    records = client.get(overview.context["org_records_url"])
    assert records.context["org_view"] == "records"
