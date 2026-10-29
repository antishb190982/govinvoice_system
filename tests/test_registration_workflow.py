from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import OTP_SESSION_KEY
from core.models import Notification
from imports.models import ImportBatch, RegistrationRequest
from imports.registration import registration_url, visible_requests
from imports.services import commit, stage_spreadsheet
from invoices.models import Invoice
from org.models import Role
from providers.models import ServiceAccount
from tests.conftest import make_user
from tests.test_bulk_intake import csv_file

pytestmark = pytest.mark.django_db


def sign_in(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def missing_account_file():
    original = csv_file("REGISTRATION-1")
    return SimpleUploadedFile(original.name, original.read().replace(b"ACC-1", b"NEW-ACCOUNT-1"))


@pytest.fixture
def registration_batch(people, provider):
    batch = stage_spreadsheet(people["clerk"], missing_account_file(), provider)
    batch.registration_mode = True
    batch.save(update_fields=["registration_mode"])
    return batch


def route_request(client, batch, unit):
    return client.post(registration_url([batch]), {
        "action": "request", "batch": batch.pk, f"batch-{batch.pk}-org_unit": unit.pk,
    })


def test_first_time_folder_routes_to_saved_registration(client, people, provider):
    sign_in(client, people["clerk"])
    uploaded = missing_account_file()
    response = client.post(reverse("imports:list"), {
        "provider": provider.pk, "received_date": timezone.localdate().isoformat(),
        "intake_route": "registration", "folder_files": [uploaded],
        "folder_paths": f'["Department/First intake/{uploaded.name}"]',
    })
    batch = ImportBatch.objects.get()
    assert response.status_code == 302 and response.url == registration_url([batch])
    assert batch.registration_mode and not Invoice.objects.exists()
    assert b"Account registration in progress" in client.get(reverse("imports:batch", args=[batch.pk])).content
    screen = client.get(response.url)
    assert screen.status_code == 200
    missing = screen.context["bundles"][0]["missing"]
    assert missing[0]["account_number"] == "NEW-ACCOUNT-1"
    assert not missing[0]["register_url"]  # clerk cannot register directly
    assert batch.original_name == "Department/First intake/REGISTRATION-1.csv"
    with pytest.raises(ValidationError, match="registration"):
        commit(batch, people["clerk"])
    assert client.get(response.url).context["bundles"][0]["batch"].file.name == batch.file.name


def test_routing_is_deduplicated_and_scoped(client, registration_batch, people, tree):
    sign_in(client, people["clerk"])
    batch = registration_batch
    assert route_request(client, batch, tree["hosp"]).status_code == 302
    assert route_request(client, batch, tree["hosp"]).status_code == 302
    item = RegistrationRequest.objects.get()
    assert item.requested_by == people["clerk"] and item.org_unit == tree["hosp"]
    assert Notification.objects.filter(user=people["admin"], dedupe_key__startswith="intake-registration:").count() == 1
    assert not Notification.objects.filter(user=people["outsider"]).exists()
    bad_route = route_request(client, batch, tree["moe"])
    assert bad_route.status_code == 200 and bad_route.context["bundles"][0]["routing_form"].errors
    item.refresh_from_db()
    assert item.org_unit == tree["hosp"]


def test_administrator_registration_returns_rechecks_and_unblocks_clerk(
        client, registration_batch, people, tree, provider):
    batch = registration_batch
    original_file, original_digest = batch.file.name, batch.sha256
    sign_in(client, people["clerk"])
    route_request(client, batch, tree["hosp"])
    item = RegistrationRequest.objects.get()
    detail_url = reverse("imports:registration_request", args=[item.pk])
    assert client.get(reverse("providers:account_create")).status_code == 403
    assert client.post(detail_url, {"action": "recheck"}).status_code == 403

    sign_in(client, people["admin"])
    queue = client.get(reverse("imports:registration_queue"))
    assert item in queue.context["requests"]
    detail = client.get(detail_url)
    link = detail.context["missing"][0]["register_url"]
    query = parse_qs(urlsplit(link).query)
    assert query["next"] == [detail_url] and query["org_unit"] == [str(tree["hosp"].pk)]
    account_screen = client.get(link)
    assert account_screen.context["form"].initial["account_number"] == "NEW-ACCOUNT-1"
    saved = client.post(link, {
        "provider": provider.pk, "org_unit": tree["hosp"].pk,
        "account_number": "NEW-ACCOUNT-1", "description": "First-time intake", "is_active": "on",
    })
    assert saved.status_code == 302, getattr(saved, "context", None)
    assert saved.url == detail_url
    item.refresh_from_db()
    batch.refresh_from_db()
    assert item.status == RegistrationRequest.Status.READY and item.completed_by == people["admin"]
    assert batch.rows.get().status == "valid"
    assert batch.file.name == original_file and batch.sha256 == original_digest
    assert not Invoice.objects.exists()
    assert client.post(reverse("imports:commit", args=[batch.pk])).status_code == 404
    assert Notification.objects.filter(user=people["clerk"], dedupe_key__startswith="intake-registration-ready:").exists()

    sign_in(client, people["clerk"])
    review = client.get(reverse("imports:batch", args=[batch.pk]))
    assert b"Account registration complete" in review.content
    assert b"Account registration in progress" not in review.content
    created, failed = commit(batch, people["clerk"])
    assert failed == 0 and len(created) == 1 and created[0].status == Invoice.Status.DRAFT
    assert created[0].service_account.account_number == "NEW-ACCOUNT-1"


def test_outside_unit_admin_cannot_view_evidence_or_mutate_request(
        client, registration_batch, people, tree):
    sign_in(client, people["clerk"])
    route_request(client, registration_batch, tree["hosp"])
    item = RegistrationRequest.objects.get()
    row = registration_batch.rows.get()
    source_url = reverse("imports:registration_source", args=[item.pk, row.pk])
    outsider_admin = make_user("other-unit-admin", [(Role.UNIT_ADMIN, tree["moe"])])
    sign_in(client, outsider_admin)
    detail = reverse("imports:registration_request", args=[item.pk])
    assert client.get(detail).status_code == 404
    assert client.post(detail, {"action": "recheck"}).status_code == 404
    assert client.get(source_url).status_code == 404
    assert not visible_requests(outsider_admin).exists()
    assert client.get(registration_url([registration_batch])).status_code == 404
    sign_in(client, people["admin"])
    evidence = client.get(source_url)
    assert evidence.status_code == 200 and "no-store" in evidence["Cache-Control"]
    assert b"NEW-ACCOUNT-1" in b"".join(evidence.streaming_content)
    root = make_user("system-admin", superuser=True)
    assert visible_requests(root).filter(pk=item.pk).exists()


def test_unidentified_provider_can_be_resolved_without_account_permission_bypass(
        client, people, provider, tree):
    batch = stage_spreadsheet(people["clerk"], missing_account_file(), None)
    sign_in(client, people["clerk"])
    route_request(client, batch, tree["hosp"])
    item = RegistrationRequest.objects.get()
    detail = reverse("imports:registration_request", args=[item.pk])
    assert client.post(detail, {"action": "provider", "provider-provider": provider.pk}).status_code == 403
    sign_in(client, people["admin"])
    assert client.post(detail, {"action": "provider", "provider-provider": provider.pk}).status_code == 302
    batch.refresh_from_db()
    assert batch.provider == provider
    assert batch.rows.get().data["unregistered_account"] == "NEW-ACCOUNT-1"
    assert not ServiceAccount.objects.filter(account_number="NEW-ACCOUNT-1").exists()


def test_invalid_provider_selection_stays_visible(client, registration_batch, people):
    sign_in(client, people["clerk"])
    batch = registration_batch
    result = client.post(registration_url([batch]), {
        "action": "provider", "batch": batch.pk, f"provider-{batch.pk}-provider": "999999",
    })
    assert result.status_code == 200 and result.context["bundles"][0]["provider_form"].errors


def test_clerk_recheck_cannot_release_administrator_handoff(client, registration_batch, people, tree, provider):
    sign_in(client, people["clerk"])
    route_request(client, registration_batch, tree["hosp"])
    ServiceAccount.objects.create(provider=provider, org_unit=tree["hosp"], account_number="NEW-ACCOUNT-1")
    client.post(registration_url([registration_batch]), {"action": "recheck", "batch": registration_batch.pk})
    item = RegistrationRequest.objects.get()
    assert item.status == RegistrationRequest.Status.REQUESTED
    registration_batch.refresh_from_db()
    assert registration_batch.rows.get().status == "valid"
    with pytest.raises(ValidationError, match="registration"):
        commit(registration_batch, people["clerk"])
    assert not Invoice.objects.exists()
    sign_in(client, people["admin"])
    client.post(reverse("imports:registration_request", args=[item.pk]), {"action": "recheck"})
    item.refresh_from_db()
    assert item.status == RegistrationRequest.Status.READY


def test_repeated_accounts_are_registered_once(people, provider):
    original = missing_account_file()
    header, row = original.read().splitlines()
    second = row.replace(b"REGISTRATION-1", b"REGISTRATION-2")
    content = SimpleUploadedFile("folder.csv", b"\n".join([header, row, second]))
    batch = stage_spreadsheet(people["clerk"], content, provider)
    from imports.registration import missing_requirements

    missing, pending = missing_requirements(batch)
    assert len(missing) == 1 and missing[0]["count"] == 2 and pending == 0
