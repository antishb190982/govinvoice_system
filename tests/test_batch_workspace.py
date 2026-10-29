import time

import pytest
from django.core import mail
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.action_notifications import dispatch
from core.models import AuditEvent, Notification
from core.notify import notify
from imports.models import ImportBatch
from integrations import config, sms
from integrations.models import OutboundMessage
from invoices.models import Invoice
from operations import services
from operations.models import InvoiceBatch
from operations.models import BatchMember
from operations.workspace import batches_for, counts_for

pytestmark = pytest.mark.django_db


def login(client, user):
    client.force_login(user)
    session = client.session
    session["otp_verified"] = True
    session["last_activity"] = int(time.time())
    session.save()


def new_batch(people, make_invoice, status="draft"):
    invoice = make_invoice()
    batch = services.create_batch(people["clerk"], invoice.org_unit, "QA workspace", [invoice.pk])
    Invoice.objects.filter(pk=invoice.pk).update(status=status)
    invoice.refresh_from_db()
    return batch, invoice


def test_folders_follow_states_without_changing_records(people, make_invoice):
    prep, _ = new_batch(people, make_invoice)
    work, _ = new_batch(people, make_invoice, "approved")
    rejected, _ = new_batch(people, make_invoice, "returned")
    complete, _ = new_batch(people, make_invoice, "paid")
    assert dict(batches_for(people["clerk"]).values_list("pk", "batch_folder")) == {
        prep.pk: "preparation", work.pk: "work", rejected.pk: "rejected", complete.pk: "completed",
    }
    assert counts_for(people["clerk"]) == {
        "preparation": 1, "work": 1, "rejected": 1, "completed": 1, "all": 4,
    }
    InvoiceBatch.objects.filter(pk=prep.pk).update(cancelled_at=timezone.now())
    assert counts_for(people["clerk"])["rejected"] == 2
    assert Invoice.objects.filter(status="paid").count() == 1


def test_paid_batch_with_open_dispute_stays_in_work(people, make_invoice):
    batch, invoice = new_batch(people, make_invoice, "paid")
    services.open_case(people["clerk"], invoice, "issue", "QA post-payment review")
    assert batches_for(people["clerk"]).get(pk=batch.pk).batch_folder == "work"


def test_released_member_does_not_reopen_historical_batch(people, make_invoice):
    batch, invoice = new_batch(people, make_invoice)
    services.open_case(people["clerk"], invoice, "issue", "QA correction")
    BatchMember.objects.filter(batch=batch).update(active=False)
    assert batches_for(people["clerk"]).get(pk=batch.pk).batch_folder == "completed"
    assert batch.lifecycle == "Closed — all members released"


def test_scope_and_personal_staged_imports(client, people, tree, provider, make_invoice):
    batch, _ = new_batch(people, make_invoice)
    InvoiceBatch.objects.create(reference="QA-FOREIGN", name="Hidden ministry", org_unit=tree["moe"],
        ministry=tree["moe"], provider=provider, currency="MUR", created_by=people["clerk"])
    incoming = make_invoice()
    own = ImportBatch.objects.create(uploaded_by=people["clerk"], kind="csv",
        original_name="Own staged.csv", sha256="a" * 64)
    other = ImportBatch.objects.create(uploaded_by=people["finance"], kind="csv",
        original_name="Other staged.csv", sha256="b" * 64)
    login(client, people["clerk"])
    response = client.get(reverse("operations:workspace"))
    assert response.status_code == 200
    assert response.context["folder_counts"]["all"] == 1
    assert list(response.context["imports"]) == [own]
    assert other not in response.context["imports"]
    assert incoming in response.context["received"]
    assert b"QA-FOREIGN" not in response.content
    assert str(batch.reference).encode() in response.content
    assert client.get(reverse("operations:workspace"), {"folder": "rejected"}).status_code == 200
    data = client.get(reverse("core:activity")).json()
    assert data["batch_folders"]["all"] == 1
    assert data["unread_notifications"] == people["clerk"].notifications.filter(read_at=None).count()


def test_activity_requires_login(client):
    assert client.get(reverse("core:activity")).status_code == 302


def test_polling_does_not_extend_idle_session(client, people):
    login(client, people["clerk"])
    session = client.session
    last = int(time.time()) - 20
    session["last_activity"] = last
    session.save()
    response = client.get(reverse("core:activity"))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert client.session["last_activity"] == last
    assert client.get(reverse("operations:workspace")).status_code == 200
    assert client.session["last_activity"] > last


@override_settings(SESSION_IDLE_TIMEOUT_MINUTES=1)
def test_expired_idle_session_cannot_poll(client, people):
    login(client, people["clerk"])
    session = client.session
    session["last_activity"] = int(time.time()) - 120
    session.save()
    assert client.get(reverse("core:activity")).status_code == 302
    assert "_auth_user_id" not in client.session


def test_new_batch_action_notifies_responsible_role_once(people, make_invoice):
    invoice = make_invoice()
    Invoice.objects.filter(pk=invoice.pk).update(status="approved")
    batch = services.create_batch(people["clerk"], invoice.org_unit, "QA ready for finance", [invoice.pk])
    event = AuditEvent.objects.filter(action="batch.created", object_id=str(batch.pk)).latest("sequence")
    notes = Notification.objects.filter(user=people["finance"], dedupe_key=f"action-event-{event.sequence}")
    assert notes.count() == 1
    assert notes.get().link == batch.get_absolute_url()
    dispatch(event, batch)
    assert notes.count() == 1


def test_activity_alert_rolls_back_with_batch(people, make_invoice):
    invoice = make_invoice()
    before = Notification.objects.count()
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            services.create_batch(people["clerk"], invoice.org_unit, "QA rollback", [invoice.pk])
            raise RuntimeError("rollback")
    assert Notification.objects.count() == before
    assert not InvoiceBatch.objects.exists()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_preview_and_disabled_sms_are_never_reported_sent(people, django_capture_on_commit_callbacks, monkeypatch):
    user = people["finance"]
    user.phone = "+23055550123"
    user.save(update_fields=["phone"])
    monkeypatch.setattr(config, "get", lambda key: False if key == "SMS_ENABLED" else "")
    with django_capture_on_commit_callbacks(execute=True):
        assert notify([user, user], "QA action", link="/operations/my-batches/", dedupe_key="qa-once") == 1
        assert notify([user], "QA action", link="/operations/my-batches/", dedupe_key="qa-once") == 0
    note = user.notifications.get(dedupe_key="qa-once")
    assert set(note.deliveries.values_list("channel", "status")) == {
        ("email", "skipped"), ("sms", "skipped"),
    }
    assert len(mail.outbox) == 1  # Local test inbox, not an external submission.


def test_action_sms_overrides_info_threshold(people, django_capture_on_commit_callbacks, monkeypatch):
    user = people["finance"]
    user.phone = "+23055550123"
    user.save(update_fields=["phone"])
    monkeypatch.setattr(config, "get", lambda key: {
        "SMS_ENABLED": True, "SMS_MIN_LEVEL": "critical", "SMS_COUNTRY_CODE": "+230",
    }.get(key, ""))
    calls = []
    monkeypatch.setattr(sms, "send_raw", lambda number, text: calls.append(number) or "QA-ACCEPTED")
    with django_capture_on_commit_callbacks(execute=True):
        notify([user], "QA normal action", link="/operations/my-batches/", email=False, dedupe_key="qa-sms")
    result = OutboundMessage.objects.get(notification__dedupe_key="qa-sms", channel="sms")
    assert calls == [user.phone]
    assert result.status == "sent"
    assert result.provider_reference == "QA-ACCEPTED"
    assert user.phone not in result.recipient


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_email_failure_does_not_remove_in_app_action(people, django_capture_on_commit_callbacks, monkeypatch):
    from django.core.mail import EmailMessage
    monkeypatch.setattr(EmailMessage, "send", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("QA outage")))
    with django_capture_on_commit_callbacks(execute=True):
        notify([people["finance"]], "QA outage", link="/operations/my-batches/", dedupe_key="qa-outage")
    note = people["finance"].notifications.get(dedupe_key="qa-outage")
    assert note.deliveries.get(channel="email").status == "failed"


def test_delivery_history_is_only_visible_to_its_recipient(client, people):
    note = Notification.objects.create(user=people["finance"], title="QA private delivery")
    OutboundMessage.objects.create(notification=note, channel="sms", recipient="masked",
        status="failed", error="PRIVATE GATEWAY ERROR")
    login(client, people["finance"])
    response = client.get(reverse("core:notifications"))
    assert response.status_code == 200
    assert b"Failed" in response.content
    assert b"PRIVATE GATEWAY ERROR" not in response.content
    login(client, people["clerk"])
    assert b"QA private delivery" not in client.get(reverse("core:notifications")).content


@pytest.mark.parametrize("payload", ["not-json", "{}", '{"sid": ""}', "[]"])
def test_sms_requires_provider_acceptance(monkeypatch, payload):
    monkeypatch.setattr(config, "get", lambda key: {
        "SMS_ENABLED": True, "SMS_PROVIDER": "twilio", "SMS_URL": "https://example.invalid",
        "SMS_ACCOUNT": "QA", "SMS_COUNTRY_CODE": "+230",
    }.get(key, ""))
    monkeypatch.setattr(sms, "_post", lambda *a, **kw: (201, payload))
    assert sms.send("+23055550123", "QA test only") is False
    assert OutboundMessage.objects.get(channel="sms").status == "failed"
