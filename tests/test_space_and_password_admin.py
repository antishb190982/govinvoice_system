import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import User
from core.models import AuditEvent, SpaceFolder
from org.models import Membership, Role
from tests.conftest import make_user
from tests.test_operations_control import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def administrator(tree):
    user = make_user("unit-password-admin", [(Role.UNIT_ADMIN, tree["moh"])])
    user.set_password("Admin-Verify-2026!")
    user.save()
    return user


def password_data(mode="reset"):
    return {"mode": mode, "new_password1": "Temporary-Cedar-2026!42",
        "new_password2": "Temporary-Cedar-2026!42", "administrator_password": "Admin-Verify-2026!",
        "confirm": "on"}


@pytest.mark.parametrize("mode,forced", [("reset", True), ("set", False)])
def test_admin_can_reset_or_set_password_without_changing_2fa_or_leaking_secrets(
        client, administrator, people, mode, forced):
    target = people["clerk"]
    old_totp = target.totp_secret
    old_session = Client()
    login(old_session, target)
    login(client, administrator)
    url = reverse("accounts:password_admin", args=[target.pk])
    assert client.get(url).status_code == 200
    response = client.post(url, password_data(mode))
    assert response.status_code == 302
    target.refresh_from_db()
    assert target.check_password(password_data()["new_password1"])
    assert target.must_change_password == forced
    assert target.totp_secret == old_totp
    assert old_session.get(reverse("core:my_work")).status_code == 302
    event = AuditEvent.objects.filter(action="user.password_reset" if forced else "user.password_set").latest("pk")
    assert "Temporary-Cedar" not in str(event.data)
    assert "Admin-Verify" not in str(event.data)


def test_password_change_requires_admin_password_and_confirmation(client, administrator, people):
    target = people["clerk"]
    old_hash = target.password
    login(client, administrator)
    url = reverse("accounts:password_admin", args=[target.pk])
    data = password_data()
    data["administrator_password"] = "wrong"
    response = client.post(url, data)
    assert "administrator_password" in response.context["form"].errors
    data = password_data()
    data.pop("confirm")
    response = client.post(url, data)
    assert "confirm" in response.context["form"].errors
    target.refresh_from_db()
    assert target.password == old_hash


def test_cross_unit_staff_self_and_unprivileged_password_paths_are_blocked(client, administrator, people, tree):
    target = people["clerk"]
    login(client, administrator)
    Membership.objects.create(user=target, org_unit=tree["moe"], role=Role.CLERK)
    assert client.get(reverse("accounts:password_admin", args=[target.pk])).status_code == 403
    assert client.post(reverse("accounts:password_admin", args=[target.pk]), password_data()).status_code == 403
    assert client.get(reverse("accounts:password_admin", args=[administrator.pk])).status_code == 403
    login(client, people["finance"])
    assert client.get(reverse("accounts:password_admin", args=[target.pk])).status_code == 403


@pytest.mark.parametrize("tab", ["overview", "providers", "batches", "invoices", "services", "tasks", "folders"])
def test_my_space_tabs_have_real_scoped_records(client, people, make_invoice, tab):
    invoice = make_invoice()
    login(client, people["clerk"])
    response = client.get(reverse("core:my_space"), {"tab": tab})
    if tab == "tasks":
        assert response.status_code == 302
        assert reverse("core:my_work") in response.url
        return
    assert response.status_code == 200
    assert response.context["tab"] == tab
    assert response.context["cards"]
    if tab == "invoices":
        assert any(row["title"] == invoice.reference for row in response.context["rows"])
    if tab == "services":
        assert any(row["title"] == invoice.service_account.account_number for row in response.context["rows"])


def test_folder_links_persist_scope_are_private_and_unlink_never_deletes_records(
        client, people, tree, provider, account, make_invoice):
    invoice = make_invoice()
    login(client, people["clerk"])
    data = {"label": "CEB preparation", "destination": "preparation", "org_unit": tree["moh"].pk,
            "provider": provider.pk, "notes": "My saved view"}
    assert client.post(reverse("core:space_folder_new"), data).status_code == 302
    folder = SpaceFolder.objects.get(owner=people["clerk"])
    response = client.get(reverse("core:my_space"), {"tab": "folders"})
    link = response.context["folders"][0]["url"]
    assert "folder=preparation" in link and f"provider={provider.pk}" in link and f"org_unit={tree['moh'].pk}" in link
    assert client.get(link).status_code == 200
    data["label"] = "Renamed personal view"
    assert client.post(reverse("core:space_folder_edit", args=[folder.pk]), data).status_code == 302
    same_unit = make_user("other-space-user", [(Role.CLERK, tree["moh"])])
    login(client, same_unit)
    assert client.get(reverse("core:space_folder_edit", args=[folder.pk])).status_code == 404
    assert client.post(reverse("core:space_folder_remove", args=[folder.pk]), {"confirm": "1"}).status_code == 404
    assert client.get(reverse("core:my_space"), {"tab": "folders"}).context["folders"] == []
    login(client, people["clerk"])
    remove = reverse("core:space_folder_remove", args=[folder.pk])
    assert client.get(remove).status_code == 405
    client.post(remove, {})
    assert SpaceFolder.objects.filter(pk=folder.pk).exists()
    client.post(remove, {"confirm": "1"})
    assert not SpaceFolder.objects.filter(pk=folder.pk).exists()
    invoice.refresh_from_db()
    assert invoice.pk


def test_folder_cannot_save_foreign_unit_and_loses_link_when_access_removed(client, people, tree):
    login(client, people["clerk"])
    response = client.post(reverse("core:space_folder_new"), {"label": "Foreign", "destination": "work",
        "org_unit": tree["moe"].pk})
    assert "org_unit" in response.context["form"].errors
    folder = SpaceFolder.objects.create(owner=people["clerk"], label="Obsolete access",
        destination="work", org_unit=tree["moe"])
    response = client.get(reverse("core:my_space"), {"tab": "folders"})
    assert response.context["folders"][0]["url"] == ""


def test_password_reauthentication_is_rate_limited(client, administrator, people, settings):
    settings.LOGIN_MAX_FAILURES = 1
    login(client, administrator)
    data = password_data()
    data["administrator_password"] = "wrong"
    url = reverse("accounts:password_admin", args=[people["clerk"].pk])
    assert client.post(url, data).status_code == 200
    assert client.post(url, password_data()).status_code == 429


def test_space_batch_pending_counts_group_invoices_and_follow_finance_roles(client, people, tree, make_invoice):
    from operations.services import create_batch
    first, second = make_invoice(), make_invoice()
    create_batch(people["clerk"], tree["moh"], "Personal pending batch", [first.pk, second.pk])
    for invoice in [first, second]:
        invoice.status = "in_approval"
        invoice.save()
    login(client, people["clerk"])
    response = client.get(reverse("core:my_space"))
    pending = next(card for card in response.context["cards"] if card["label"] == "Batches pending approval")
    assert pending["count"] == 1
    listed = client.get(pending["url"])
    assert listed.context["page"].paginator.count == 1
    assert listed.context["folder_filter"] == "pending"
    outsider = make_user("unassigned-space-colleague", [(Role.CLERK, tree["moh"])])
    login(client, outsider)
    assert client.get(reverse("core:my_space"), {"tab": "batches"}).context["page"].paginator.count == 0
    for invoice in [first, second]:
        invoice.status = "approved"
        invoice.save()
    login(client, people["finance"])
    response = client.get(reverse("core:my_space"), {"tab": "batches"})
    assert response.context["page"].paginator.count == 1
