import pytest
from django.urls import reverse
from accounts.middleware import OTP_SESSION_KEY
from providers.models import ServiceAccount

pytestmark = pytest.mark.django_db


def sign_in(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def test_account_register_filters_drive_dashboard_and_drilldown(
        client, people, provider, account, tree):
    account.service_type = "Electricity"
    account.status = ServiceAccount.Status.ACTIVE
    account.save()
    sign_in(client, people["clerk"])
    query = {
        "provider": str(provider.pk), "ministry": str(tree["moh"].pk),
        "service_type": "Electricity", "status": "active",
        "invoicing_status": "no_invoices",
    }
    response = client.get(reverse("providers:accounts"), query)
    assert response.status_code == 200
    assert response.context["filter_form"].errors == {}, response.context["filter_form"].errors
    assert account.pk in {item.pk for item in response.context["page"].object_list}
    assert response.context["account_total"] >= 1
    card = next(c for c in response.context["account_dashboard_cards"]
                if c["label"] == "No invoice recorded")
    assert card["count"] >= 1
    assert "provider=" in card["url"] and "service_type=Electricity" in card["url"]

    overview = client.get(reverse("core:section_overview", args=["accounts"]), query)
    assert overview.status_code == 200
    assert overview.context["cards"][0]["value"] == response.context["account_total"]
    provider_dashboard = client.get(reverse("providers:visibility"), {"utility_provider": provider.pk})
    assert provider_dashboard.status_code == 200
    assert provider_dashboard.context["utility_provider"] == provider
    assert b"Monthly billed trend" in provider_dashboard.content


def test_account_register_filters_do_not_expose_another_ministry(client, people, provider, tree):
    foreign = ServiceAccount.objects.create(provider=provider, org_unit=tree["moe"],
        account_number="FOREIGN-ONLY", service_type="Electricity")
    sign_in(client, people["clerk"])
    response = client.get(reverse("providers:accounts"), {"q": foreign.account_number})
    assert response.status_code == 200
    assert foreign.pk not in {item.pk for item in response.context["page"].object_list}
