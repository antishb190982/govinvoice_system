import pytest
from django.test import Client
from django.urls import reverse

from core.models import AuditEvent, SpaceFolder
from tests.test_operations_control import login

pytestmark = pytest.mark.django_db


def test_pin_unpin_are_persistent_private_and_keep_saved_filters(client, people, tree, provider, account):
    owner = people["clerk"]
    folder = SpaceFolder.objects.create(owner=owner, label="My priority electricity",
        destination="preparation", org_unit=tree["moh"], provider=provider)
    login(client, owner)
    url = reverse("core:space_folder_pin", args=[folder.pk])
    assert client.get(url).status_code == 405
    assert client.post(url, {"pinned": "1"}).status_code == 302
    folder.refresh_from_db()
    assert folder.is_pinned
    response = client.get(reverse("core:dashboard"))
    pinned = response.context["pinned_space_folders"]
    assert len(pinned) == 1 and pinned[0]["pk"] == folder.pk
    assert f"provider={provider.pk}" in pinned[0]["url"]
    assert f"org_unit={tree['moh'].pk}" in pinned[0]["url"]
    assert "folder=preparation" in pinned[0]["url"]
    assert b'aria-label="Pinned My Space folders"' in response.content
    assert client.get(pinned[0]["url"], follow=True).status_code == 200

    # Reloading in a different browser session for the same user keeps the pin.
    second = Client()
    login(second, owner)
    assert len(second.get(reverse("core:my_space")).context["pinned_space_folders"]) == 1
    client.post(url, {"pinned": "1"})
    assert AuditEvent.objects.filter(action="space.folder_pinned", actor=owner).count() == 1
    login(client, people["finance"])
    assert client.post(url, {"pinned": "0"}).status_code == 404
    assert client.get(reverse("core:my_space")).context["pinned_space_folders"] == []

    login(client, owner)
    assert client.post(url, {"pinned": "0"}).status_code == 302
    folder.refresh_from_db()
    assert not folder.is_pinned and folder.provider == provider
    assert SpaceFolder.objects.filter(pk=folder.pk).exists()
    assert client.get(reverse("core:dashboard")).context["pinned_space_folders"] == []


def test_pin_state_can_be_saved_on_form_and_is_removed_with_shortcut(client, people):
    login(client, people["clerk"])
    response = client.post(reverse("core:space_folder_new"),
        {"label": "My ongoing work", "destination": "work", "is_pinned": "on"})
    assert response.status_code == 302
    folder = SpaceFolder.objects.get(owner=people["clerk"])
    assert folder.is_pinned
    page = client.get(reverse("core:my_space"), {"tab": "folders"})
    assert b">Unpin</button>" in page.content
    assert client.post(reverse("core:space_folder_remove", args=[folder.pk]), {"confirm": "1"}).status_code == 302
    assert client.get(reverse("core:dashboard")).context["pinned_space_folders"] == []


def test_unavailable_scope_never_appears_in_navigation_or_accepts_pin(client, people, tree):
    login(client, people["clerk"])
    folder = SpaceFolder.objects.create(owner=people["clerk"], label="Access no longer available",
        destination="work", org_unit=tree["moe"], is_pinned=True)
    assert client.get(reverse("core:dashboard")).context["pinned_space_folders"] == []
    endpoint = reverse("core:space_folder_pin", args=[folder.pk])
    response = client.post(endpoint, {"pinned": "1"}, follow=True)
    assert b"no longer available" in response.content
    assert client.post(endpoint, {"pinned": "0"}).status_code == 302
    folder.refresh_from_db()
    assert not folder.is_pinned
    assert client.post(endpoint, {"pinned": "unexpected"}).status_code == 302
    folder.refresh_from_db()
    assert not folder.is_pinned


def test_pin_endpoint_requires_csrf(client, people):
    folder = SpaceFolder.objects.create(owner=people["clerk"], label="Protected", destination="all")
    protected = Client(enforce_csrf_checks=True)
    login(protected, people["clerk"])
    assert protected.post(reverse("core:space_folder_pin", args=[folder.pk]), {"pinned": "1"}).status_code == 403
    folder.refresh_from_db()
    assert not folder.is_pinned
