import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from accounts.middleware import OTP_SESSION_KEY
from accounts.models import User
from core.demo_setup import load_demo_reference_data
from core.models import AuditEvent
from invoices.models import Invoice
from org.models import OrgUnit
from providers.models import Provider, ServiceAccount
from tests.conftest import make_user
from workflow.models import ApprovalPolicy

pytestmark = pytest.mark.django_db


def authenticate(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def test_demo_setup_is_additive_and_repeatable_on_published_instance(tree, settings):
    settings.PRODUCTION = True
    admin = make_user("demo-loader-admin", superuser=True)
    password = admin.password
    first = load_demo_reference_data(admin)
    assert first["providers"] == 5 and first["accounts"] > 0
    assert all(account.account_number.startswith("DEMO-") for account in ServiceAccount.objects.all())
    assert not any(provider.bank_iban or provider.bank_name for provider in Provider.objects.all())
    assert Invoice.objects.count() == 0 and User.objects.count() == 1
    counts = (OrgUnit.objects.count(), Provider.objects.count(), ServiceAccount.objects.count(),
              ApprovalPolicy.objects.count())
    assert not any(load_demo_reference_data(admin).values())
    assert counts == (OrgUnit.objects.count(), Provider.objects.count(), ServiceAccount.objects.count(),
                      ApprovalPolicy.objects.count())
    admin.refresh_from_db()
    assert admin.password == password
    assert AuditEvent.objects.filter(action="demo.reference_data_loaded").count() == 2


def test_existing_provider_alias_and_service_account_are_preserved(tree, provider, account):
    admin = make_user("demo-loader-admin", superuser=True)
    provider.short_code, provider.name = "C.E.B", "Central Electricity Board"
    provider.save()
    previous_bank = provider.bank_iban
    previous_unit = account.org_unit_id
    created = load_demo_reference_data(admin)
    assert created["providers"] == 4
    assert not Provider.objects.filter(short_code="CEB").exists()
    provider.refresh_from_db()
    account.refresh_from_db()
    assert provider.bank_iban == previous_bank
    assert account.org_unit_id == previous_unit and account.account_number == "ACC-1"
    assert ServiceAccount.objects.filter(provider=provider, account_number__startswith="DEMO-CEB-").exists()


def test_ambiguous_provider_matches_roll_back_entire_load(tree, provider):
    admin = make_user("demo-loader-admin", superuser=True)
    Provider.objects.create(name="Another electric company", short_code="CEB", utility_type="electricity")
    provider.name = "Central Electricity Board"
    provider.save()
    original_units = OrgUnit.objects.count()
    with pytest.raises(ValidationError, match="More than one"):
        load_demo_reference_data(admin)
    assert OrgUnit.objects.count() == original_units
    assert ServiceAccount.objects.count() == 0
    assert not AuditEvent.objects.filter(action="demo.reference_data_loaded").exists()


def test_only_system_administrators_can_load_demo_data(tree, people):
    with pytest.raises(PermissionDenied):
        load_demo_reference_data(people["admin"])
    assert Provider.objects.count() == 0


def test_admin_confirmation_is_required_and_get_does_not_write(client, tree):
    admin = make_user("demo-loader-admin", superuser=True)
    authenticate(client, admin)
    url = reverse("console:demo_setup")
    assert client.get(url).status_code == 200
    assert client.post(url, {}).status_code == 200
    assert Provider.objects.count() == 0
    response = client.post(url, {"confirm_demo": "on"})
    assert response.status_code == 302
    assert Provider.objects.count() == 5
    assert b"Demo setup loaded:" in client.get(response.url).content


def test_demo_setup_rejects_non_admin_and_requires_csrf(client, tree, people):
    authenticate(client, people["admin"])
    url = reverse("console:demo_setup")
    assert client.get(url).status_code == 403
    assert client.post(url, {"confirm_demo": "on"}).status_code == 403
    admin = make_user("demo-loader-admin", superuser=True)
    protected = Client(enforce_csrf_checks=True)
    authenticate(protected, admin)
    assert protected.post(url, {"confirm_demo": "on"}).status_code == 403
    assert Provider.objects.count() == 0
