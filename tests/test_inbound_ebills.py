import hashlib
from datetime import timedelta
from email.message import EmailMessage
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.models import AuditEvent
from imports.models import ImportBatch
from integrations.inbound import process_delivery
from integrations.inbound_models import EbillDelivery, EbillMailbox, EbillSender
from integrations.models import ApiClient
from invoices.models import Invoice
from tests.test_operations_control import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def mail_setup(people, provider, account, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "mail-media"
    admin = User.objects.create_superuser(username="inbound-admin", password="test-only-mail-admin")
    relay = ApiClient(name="Dedicated e-bill relay", scopes=["mail:ingest"],
                      service_user=people["clerk"], created_by=admin)
    token = relay.issue_token()
    relay.save()
    mailbox = EbillMailbox.objects.create(name="Utility bills", address="bills@example.test",
                                          enabled=True, relay_client=relay)
    sender = EbillSender.objects.create(mailbox=mailbox, address="invoices@power.example",
                                        provider=provider)
    message = EmailMessage()
    message["From"] = "Power billing <invoices@power.example>"
    message["To"] = mailbox.address
    message["Subject"] = "Electricity e-bill"
    message["Message-ID"] = "<mail-1@power.example>"
    message["Date"] = "Thu, 01 Jan 2004 00:00:00 +0000"  # Sender-controlled, not the receipt date.
    message.set_content("Please process the attached utility invoice.")
    csv = ("account_number,invoice_number,issue_date,due_date,total,currency\n"
           f"{account.account_number},MAIL-TEST-1,2026-09-01,2026-10-01,100,MUR\n").encode()
    message.add_attachment(csv, maintype="text", subtype="csv", filename="invoice.csv")
    return {"relay": relay, "token": token, "mailbox": mailbox, "rule": sender,
            "raw": message.as_bytes(), "admin": admin}


def receive(client, setup, **changes):
    payload = {
        "mailbox": setup["mailbox"].address, "delivery_id": "delivery-001",
        "received_at": timezone.now().isoformat(), "authenticated_sender": setup["rule"].address,
        "authentication_passed": "true", "attachments_clean": "true",
        "message_sha256": hashlib.sha256(setup["raw"]).hexdigest(),
        "message": SimpleUploadedFile("bill.eml", setup["raw"], "message/rfc822"),
    }
    payload.update(changes)
    return client.post(reverse("api:mail_receive"), payload,
                       HTTP_AUTHORIZATION="Bearer " + setup["token"])


def test_trusted_authorised_mail_stages_once_and_never_approves(client, mail_setup):
    response = receive(client, mail_setup)
    assert response.status_code == 200, response.content
    assert response.json()["state"] == "staged", response.content
    delivery = EbillDelivery.objects.get()
    assert delivery.import_batches.count() == 1
    batch = delivery.import_batches.get()
    assert batch.uploaded_by_id == mail_setup["relay"].service_user_id
    assert not Invoice.objects.exists()
    assert delivery.provider_id == mail_setup["rule"].provider_id
    assert batch.rows.first().data["received_date"] == timezone.localdate().isoformat()
    repeated = receive(client, mail_setup)
    assert repeated.json()["duplicate"] is True
    assert EbillDelivery.objects.count() == 1
    assert ImportBatch.objects.count() == 1
    assert AuditEvent.objects.filter(action="mail.invoices_staged").count() == 1


@pytest.mark.parametrize("changes,expected", [
    ({"authenticated_sender": "spoof@elsewhere.example"}, "rejected"),
    ({"authentication_passed": "false"}, "rejected"),
    ({"attachments_clean": "false"}, "quarantined"),
])
def test_unverified_messages_never_reach_invoice_parsers(client, mail_setup, changes, expected):
    with patch("integrations.inbound.stage_uploads") as stage:
        response = receive(client, mail_setup, **changes)
    assert response.status_code == 200
    assert response.json()["state"] == expected
    stage.assert_not_called()
    assert not ImportBatch.objects.exists()


def test_unknown_sender_and_disabled_rules_are_rejected(client, mail_setup):
    mail_setup["rule"].enabled = False
    mail_setup["rule"].save()
    with patch("integrations.inbound.stage_uploads") as stage:
        response = receive(client, mail_setup)
    assert response.json()["state"] == "rejected"
    stage.assert_not_called()


def test_disabled_mailbox_wrong_token_and_conflicting_replay(client, mail_setup):
    mailbox = mail_setup["mailbox"]
    mailbox.enabled = False
    mailbox.save()
    assert receive(client, mail_setup).status_code == 403
    assert not EbillDelivery.objects.exists()
    mailbox.enabled = True
    mailbox.save()
    assert client.post(reverse("api:mail_receive"), {}).status_code == 401
    assert receive(client, mail_setup).status_code == 200
    altered = mail_setup["raw"] + b"\nChanged contents"
    conflict = receive(client, mail_setup,
        message=SimpleUploadedFile("bill.eml", altered),
        message_sha256=hashlib.sha256(altered).hexdigest())
    assert conflict.status_code == 409
    assert EbillDelivery.objects.count() == 1
    assert ImportBatch.objects.count() == 1


def test_manual_hold_can_stage_but_cannot_override_sender_or_scan(client, mail_setup):
    mailbox = mail_setup["mailbox"]
    mailbox.auto_stage = False
    mailbox.save()
    assert receive(client, mail_setup).json()["state"] == "held"
    delivery = EbillDelivery.objects.get()
    assert not ImportBatch.objects.exists()
    result = process_delivery(delivery.pk, manual=True, actor=mail_setup["admin"])
    assert result.state == "staged"
    assert ImportBatch.objects.count() == 1
    unscanned = receive(client, mail_setup, delivery_id="unscanned", attachments_clean="false")
    blocked = process_delivery(unscanned.json()["id"], manual=True, actor=mail_setup["admin"])
    assert blocked.state == "quarantined"
    assert ImportBatch.objects.count() == 1


def test_admin_forms_mapping_history_and_original_are_permission_checked(client, mail_setup, people, provider):
    login(client, people["clerk"])
    assert client.get(reverse("console:incoming")).status_code == 403
    login(client, mail_setup["admin"])
    page = client.get(reverse("console:incoming_mailbox", args=[mail_setup["mailbox"].pk]))
    assert page.status_code == 200
    assert b"No Outlook account is connected" in page.content
    assert b'id="id_mailbox-address"' in page.content
    assert b'id="id_sender-address"' in page.content
    assert b'name="utility_provider"' not in page.content
    update = client.post(reverse("console:incoming_rule", args=[mail_setup["mailbox"].pk, mail_setup["rule"].pk]), {
        "action": "save_sender", "sender-address": "NEW@POWER.EXAMPLE",
        "sender-provider": provider.pk, "sender-enabled": "on"})
    assert update.status_code == 302
    mail_setup["rule"].refresh_from_db()
    assert mail_setup["rule"].address == "new@power.example"
    # Restore the allowlist entry for the genuine sender.
    mail_setup["rule"].address = "invoices@power.example"
    mail_setup["rule"].save()
    response = receive(client, mail_setup)
    pk = response.json()["id"]
    detail = client.get(reverse("console:incoming_delivery", args=[pk]))
    assert detail.status_code == 200
    download = client.get(reverse("console:incoming_download", args=[pk]))
    assert download.status_code == 200
    assert download["Content-Disposition"].startswith("attachment")
    download.close()
    login(client, people["clerk"])
    assert client.get(reverse("console:incoming_download", args=[pk])).status_code == 403
    assert client.post(reverse("console:incoming_retry", args=[pk])).status_code == 403


def test_mailbox_cannot_be_enabled_without_a_dedicated_relay(client, mail_setup):
    login(client, mail_setup["admin"])
    response = client.post(reverse("console:incoming"), {
        "action": "save_mailbox", "mailbox-name": "Unconnected mailbox",
        "mailbox-address": "unconnected@example.test", "mailbox-enabled": "on",
        "mailbox-auto_stage": "on"})
    assert response.status_code == 400
    assert not EbillMailbox.objects.filter(address="unconnected@example.test").exists()
    relay = mail_setup["relay"]
    relay.is_active = False
    relay.save()
    # An expired or revoked client must not prevent an administrator disabling intake.
    disabled = client.post(reverse("console:incoming_mailbox", args=[mail_setup["mailbox"].pk]), {
        "action": "save_mailbox", "mailbox-name": "Utility bills",
        "mailbox-address": mail_setup["mailbox"].address, "mailbox-relay_client": relay.pk})
    assert disabled.status_code == 302
    mail_setup["mailbox"].refresh_from_db()
    assert not mail_setup["mailbox"].enabled


def test_admin_can_create_a_working_single_purpose_relay_client(client, mail_setup, people):
    login(client, mail_setup["admin"])
    page = client.get(reverse("console:api"))
    assert page.status_code == 200
    assert b'name="capture_user"' in page.content
    response = client.post(reverse("console:api"), {
        "name": "Mailbox-specific relay", "scopes": ["mail:ingest"],
        "capture_user": people["clerk"].pk, "rate_limit_per_minute": "30",
        "expires_at": (timezone.localdate() + timedelta(days=30)).isoformat()})
    assert response.status_code == 200
    created = ApiClient.objects.get(name="Mailbox-specific relay")
    assert created.service_user_id == people["clerk"].pk
    assert created.scopes == ["mail:ingest"]
    client.post(reverse("console:api"), {
        "name": "Normal read-only client", "scopes": ["invoices:read"],
        "rate_limit_per_minute": "30"})
    ordinary = ApiClient.objects.get(name="Normal read-only client")
    assert not ordinary.service_user.is_active
    assert ordinary.scopes == ["invoices:read"]


def test_relay_requires_one_message_and_integrity_bound_metadata(client, mail_setup):
    extra = receive(client, mail_setup, extra_file=SimpleUploadedFile("extra.txt", b"unexpected"))
    assert extra.status_code == 400
    invalid_digest = receive(client, mail_setup, message_sha256="0" * 64)
    assert invalid_digest.status_code == 400
    future = receive(client, mail_setup, received_at=(timezone.now() + timedelta(days=1)).isoformat())
    assert future.status_code == 400
    assert not EbillDelivery.objects.exists()


def test_capture_identity_cannot_exceed_the_relay_scope(client, mail_setup, tree):
    relay = mail_setup["relay"]
    relay.org_unit = tree["moe"]  # The processor is a Ministry of Health clerk.
    relay.save()
    with patch("integrations.inbound.stage_uploads") as stage:
        response = receive(client, mail_setup)
    assert response.status_code == 403
    stage.assert_not_called()
    assert not EbillDelivery.objects.exists()
