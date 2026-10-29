import pytest
from django.urls import reverse

from tests.test_operations_control import login

pytestmark = pytest.mark.django_db


def test_sign_in_uses_approved_workspace_theme(client):
    response = client.get(reverse("accounts:login"))
    assert response.status_code == 200
    assert b'css/workspace.css' in response.content
    assert b'data-theme="light"' in response.content


def test_authenticated_shell_keeps_real_routes_and_loads_theme_last(client, people):
    login(client, people["clerk"])
    response = client.get(reverse("core:dashboard"))
    assert response.status_code == 200
    html = response.content.decode()
    assert html.index("css/workspace.css") > html.index("css/my-space.css")
    assert "js/workspace.js" in html
    assert 'id="app-sidebar"' in html
    assert 'class="sidebar-context"' in html
    assert reverse("accounts:profile") in html
    assert reverse("core:my_space") in html
    assert "Design preview" not in html
    assert "Synthetic sample" not in html


def test_my_space_has_one_real_provider_selector_and_preserves_explicit_filters(
    client, people, provider, account
):
    login(client, people["clerk"])
    selected = client.get(reverse("core:my_space"), {"provider": provider.pk, "tab": "invoices"})
    assert selected.status_code == 200
    assert selected.content.count(b'name="provider"') == 1
    assert selected.context["utility_provider"].pk == provider.pk
    assert str(selected.context["filter_form"]["provider"].value()) == str(provider.pk)
    fresh = client.get(reverse("core:my_space"))
    assert fresh.status_code == 200
    assert fresh.context["utility_provider"] is None
    assert fresh.content.count(b'name="provider"') == 1
    assert b"Assigned work &amp; recent items" in fresh.content
    assert b"Upload Utility Records" in fresh.content


def test_saved_folder_view_retains_provider_navigation(client, people, provider, account):
    login(client, people["clerk"])
    response = client.get(reverse("core:my_space"), {"tab": "folders"})
    assert response.status_code == 200
    assert b"css/workspace.css" in response.content
    assert response.content.count(b'name="utility_provider"') == 1
