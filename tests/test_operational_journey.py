from decimal import Decimal
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from accounts.middleware import OTP_SESSION_KEY
from core.models import AuditEvent
from imports.models import ImportBatch
from invoices.models import Invoice
from operations import services
from operations.readiness import batch_readiness
from workflow import engine

pytestmark = pytest.mark.django_db


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def readiness(batch, user):
    members = list(batch.members.select_related("invoice"))
    for member in members:
        member.payable = services.payable(member.invoice)
    return batch_readiness(batch, members, user)


def test_readiness_is_read_only_and_submission_rechecks_changed_evidence(people, policy, make_invoice):
    invoice = make_invoice()
    batch = services.create_batch(people["clerk"], invoice.org_unit, "Capture once", [invoice.pk])
    before = AuditEvent.objects.count()
    result = readiness(batch, people["clerk"])
    assert result["ready"] and result["ready_count"] == 1
    assert not invoice.approval_tasks.exists()
    assert AuditEvent.objects.count() == before
    Invoice.objects.filter(pk=invoice.pk).update(amount_total=invoice.amount_total + Decimal("1"))
    assert not readiness(batch, people["clerk"])["ready"]
    with pytest.raises(ValidationError):
        services.start_batch(batch, people["clerk"])
    assert not invoice.approval_tasks.exists()
    batch.refresh_from_db()
    assert not batch.started_at


def test_readiness_lists_multiple_blockers_without_creating_new_data(people, make_invoice):
    invoice = make_invoice(reconciled=False)
    Invoice.objects.filter(pk=invoice.pk).update(document="")
    invoice.refresh_from_db()
    batch = services.create_batch(people["clerk"], invoice.org_unit, "Needs checks", [invoice.pk])
    result = readiness(batch, people["clerk"])
    assert not result["ready"] and result["blocked_count"] == 1
    reasons = " ".join(result["issues"][0]["reasons"])
    assert "reconciliation" in reasons and "document" in reasons and "policy" in reasons
    assert Invoice.objects.count() == 1


def test_batch_submission_is_atomic_when_one_invoice_loses_reconciliation(people, policy, make_invoice):
    good = make_invoice()
    bad = make_invoice(reconciled=False)
    batch = services.create_batch(people["clerk"], good.org_unit, "No partial approval", [good.pk, bad.pk])
    with pytest.raises(ValidationError):
        services.start_batch(batch, people["clerk"])
    assert not good.approval_tasks.exists()
    good.refresh_from_db()
    assert good.status == "draft"
    assert not AuditEvent.objects.filter(action="batch.submitted", object_id=str(batch.pk)).exists()


def test_reused_invoice_still_requires_independent_approval(people, policy, make_invoice):
    invoice = make_invoice()
    batch = services.create_batch(people["clerk"], invoice.org_unit, "Independent review", [invoice.pk])
    services.start_batch(batch, people["clerk"])
    invoice.refresh_from_db()
    assert invoice.status == "in_approval"
    task = invoice.approval_tasks.get(status="pending")
    with pytest.raises(PermissionDenied):
        engine.check_can_decide(task, people["clerk"])
    assert Invoice.objects.count() == 1


def test_starting_from_received_records_reuses_intake_and_existing_preparation(
        client, people, tree, provider):
    intake = ImportBatch.objects.create(uploaded_by=people["clerk"], provider=provider,
        original_name="Existing source.csv", status="committed")
    login(client, people["clerk"])
    url = reverse("operations:prepare_new")
    page = client.get(url, {"intake": intake.pk})
    assert page.status_code == 200
    assert b'name="source_intake"' in page.content
    response = client.post(url, {"source_intake": intake.pk, "name": "Reused receipt",
        "org_unit": tree["moh"].pk, "provider": provider.pk, "currency": "MUR"})
    assert response.status_code == 302 and "step=select" in response.url
    prep = intake.preparations.get()
    assert prep.imports.count() == 1 and not Invoice.objects.exists()
    again = client.get(url, {"intake": intake.pk})
    assert again.status_code == 302 and prep.get_absolute_url() in again.url
    assert intake.preparations.count() == 1


def test_intake_cannot_be_reused_by_another_user(client, people, provider):
    intake = ImportBatch.objects.create(uploaded_by=people["finance"], provider=provider,
        original_name="Private upload.csv")
    login(client, people["clerk"])
    assert client.get(reverse("operations:prepare_new"), {"intake": intake.pk}).status_code == 404
    assert not intake.preparations.exists()


def test_preparation_upload_provider_is_saved_context_not_reentered(client, people, tree, provider):
    from operations.models import BatchPreparation
    prep = BatchPreparation.objects.create(name="Saved provider", org_unit=tree["moh"],
        provider=provider, currency="MUR", prepared_by=people["clerk"])
    login(client, people["clerk"])
    response = client.get(prep.get_absolute_url())
    assert response.context["upload_form"].fields["provider"].disabled
    assert response.context["upload_form"].fields["provider"].widget.is_hidden


def test_batch_page_shows_readiness_and_does_not_enable_blocked_submit(client, people, make_invoice):
    invoice = make_invoice(reconciled=False)
    batch = services.create_batch(people["clerk"], invoice.org_unit, "Blocked", [invoice.pk])
    login(client, people["clerk"])
    response = client.get(batch.get_absolute_url())
    assert response.status_code == 200
    assert not response.context["readiness"]["ready"]
    assert b"disabled" in response.content and b"readiness" in response.content
    response = client.post(batch.get_absolute_url(), {"action": "submit"})
    assert response.status_code == 200
    assert not invoice.approval_tasks.exists()


def test_successful_import_returns_to_same_preparation_without_recapturing(
        client, people, tree, provider, account):
    from operations.models import BatchPreparation
    from imports.services import stage_spreadsheet
    from tests.test_bulk_intake import csv_file
    intake = stage_spreadsheet(people["clerk"], csv_file(), provider)
    prep = BatchPreparation.objects.create(name="One source", org_unit=tree["moh"],
        provider=provider, currency="MUR", prepared_by=people["clerk"])
    prep.imports.add(intake)
    login(client, people["clerk"])
    response = client.post(reverse("imports:commit", args=[intake.pk]))
    assert response.status_code == 302
    assert response.url == prep.get_absolute_url() + "?step=select"
    invoice = Invoice.objects.get(import_batch=intake)
    assert intake.rows.get().invoice_id == invoice.pk
    assert prep.imports.get().pk == intake.pk
    again = client.post(reverse("imports:commit", args=[intake.pk]))
    assert again.status_code == 302
    assert Invoice.objects.filter(import_batch=intake).count() == 1


def test_completed_paid_batch_is_history_not_a_false_submission_exception(people, make_invoice):
    invoice = make_invoice()
    batch = services.create_batch(people["clerk"], invoice.org_unit, "Historical batch", [invoice.pk])
    Invoice.objects.filter(pk=invoice.pk).update(status="paid")
    result = readiness(batch, people["clerk"])
    assert not result["ready"] and not result["issues"]


def test_preview_uses_current_styles_even_with_a_stale_static_collection(tmp_path):
    from config import replit_preview
    from django.test import RequestFactory, override_settings
    from django.http import HttpResponse
    from whitenoise.middleware import WhiteNoiseMiddleware
    assert replit_preview.WHITENOISE_USE_FINDERS
    assert replit_preview.WHITENOISE_AUTOREFRESH
    assert replit_preview.WHITENOISE_MAX_AGE == 0
    with override_settings(DEBUG=False, STATIC_ROOT=tmp_path,
            WHITENOISE_USE_FINDERS=True, WHITENOISE_AUTOREFRESH=True, WHITENOISE_MAX_AGE=0):
        middleware = WhiteNoiseMiddleware(lambda request: HttpResponse(status=404))
        response = middleware(RequestFactory().get("/static/css/operations.css"))
        assert response.status_code == 200
        assert b".ops-anchors" in b"".join(response.streaming_content)
