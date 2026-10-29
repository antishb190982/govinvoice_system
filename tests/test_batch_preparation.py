import pytest
from django.urls import reverse
from accounts.middleware import OTP_SESSION_KEY
from core.models import AuditEvent
from invoices.models import Invoice
from operations.models import BatchPreparation
from operations.services import start_batch
from operations import services as ops
from org.models import OrgUnit, Role
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


def sign_in(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


def upload_preparation(people, tree, provider):
    return BatchPreparation.objects.create(name="Folder intake preparation", org_unit=tree["moh"],
        provider=provider, currency="MUR", prepared_by=people["clerk"])


def test_wizard_exposes_folder_selection_and_confirmation(client, people, tree, provider):
    prep = upload_preparation(people, tree, provider)
    sign_in(client, people["clerk"])
    response = client.get(prep.get_absolute_url())
    assert response.status_code == 200
    assert b"webkitdirectory" in response.content
    assert b"folder-intake.js" in response.content
    assert b'name="confirm_processing"' in response.content
    assert b"Confirm and process invoice files" in response.content
    assert b"data-intake-summary" in response.content
    selection = client.get(prep.get_absolute_url() + "?step=select")
    assert b"Load files or a folder" in selection.content


@pytest.mark.parametrize("field", ["folder_files", "files"])
def test_wizard_does_not_process_any_files_without_confirmation(client, people, tree, provider, account, field):
    from django.utils import timezone
    from imports.models import ImportBatch
    from tests.test_bulk_intake import csv_file
    prep = upload_preparation(people, tree, provider)
    sign_in(client, people["clerk"])
    before = ImportBatch.objects.count()
    response = client.post(prep.get_absolute_url() + "?step=select", {
        "action": "upload", "provider": provider.pk, "received_date": timezone.localdate().isoformat(),
        field: csv_file(), "folder_paths": '["Invoices/October/BULK-1.csv"]' if field == "folder_files" else "",
    })
    assert response.status_code == 200
    assert response.context["step"] == "source"
    assert "confirm_processing" in response.context["upload_form"].errors
    assert ImportBatch.objects.count() == before
    assert not prep.imports.exists()
    assert not Invoice.objects.exists()
    assert not AuditEvent.objects.filter(action="batch.preparation_intake_confirmed").exists()


@pytest.mark.parametrize("registration", [False, True])
def test_confirmed_folder_intake_stages_for_review_and_records_confirmation(client, people, tree, provider, account,
                                                                          registration):
    from django.utils import timezone
    from tests.test_bulk_intake import csv_file
    prep = upload_preparation(people, tree, provider)
    sign_in(client, people["clerk"])
    response = client.post(prep.get_absolute_url(), {
        "action": "upload", "provider": provider.pk, "received_date": timezone.localdate().isoformat(),
        "folder_files": csv_file(), "folder_paths": '["Invoices/October/BULK-1.csv"]',
        "confirm_processing": "on", "intake_route": "registration" if registration else "",
    })
    assert response.status_code == 302
    assert prep.imports.count() == 1
    assert prep.imports.first().rows.count() == 1
    assert prep.imports.first().registration_mode == registration
    assert not Invoice.objects.exists()  # Intake confirmation never bypasses invoice review/commit.
    event = AuditEvent.objects.get(action="batch.preparation_intake_confirmed")
    assert event.actor == people["clerk"]
    assert event.data["folder_files"] == 1
    assert event.data["import_batches"] == [prep.imports.first().pk]


def test_folder_confirmation_does_not_bypass_path_checks(client, people, tree, provider, account):
    from django.utils import timezone
    from tests.test_bulk_intake import csv_file
    prep = upload_preparation(people, tree, provider)
    sign_in(client, people["clerk"])
    response = client.post(prep.get_absolute_url(), {
        "action": "upload", "provider": provider.pk, "received_date": timezone.localdate().isoformat(),
        "folder_files": csv_file(), "folder_paths": '["../BULK-1.csv"]', "confirm_processing": "on",
    })
    assert response.status_code == 200
    assert response.context["error"]
    assert not prep.imports.exists()


def test_wizard_selects_allocated_invoices_and_audits_both_owners(client, tree, people, provider, account,
                                                                 make_invoice):
    clerk = people["clerk"]
    prior_owner = make_user("priorowner", [(Role.CLERK, tree["moh"])])
    invoice = make_invoice()
    invoice.processing_ministry = tree["moh"]
    invoice.processing_owner = prior_owner
    invoice.save()
    # A record allocated to another ministry cannot leak into this preparation.
    foreign_account = type(account).objects.create(provider=provider, org_unit=tree["moe"],
                                                    account_number="EDU-ONLY")
    foreign = make_invoice()
    foreign.service_account = foreign_account
    foreign.org_unit = tree["moe"]
    foreign.save()

    sign_in(client, clerk)
    response = client.post(reverse("operations:prepare_new"), {
        "name": "June electricity", "org_unit": tree["moh"].pk, "provider": provider.pk, "currency": "MUR",
    })
    assert response.status_code == 302
    prep = BatchPreparation.objects.get(prepared_by=clerk)
    response = client.get(f"{prep.get_absolute_url()}?step=select")
    assert response.status_code == 200
    assert list(response.context["candidates"]) == [invoice]
    assert foreign.reference.encode() not in response.content

    response = client.post(f"{prep.get_absolute_url()}?step=select", {
        "action": "select", "invoices": [invoice.pk],
    })
    assert response.status_code == 302 and "step=confirm" in response.url
    response = client.post(prep.get_absolute_url(), {"action": "complete"})
    assert response.status_code == 302
    prep.refresh_from_db()
    batch = prep.completed_batch
    invoice.refresh_from_db()
    assert invoice.processing_ministry_id == tree["moh"].pk
    assert invoice.processing_owner_id == clerk.pk
    event = AuditEvent.objects.get(action="invoice.processing_ownership_assigned",
                                   object_id=str(invoice.pk))
    assert event.actor_id == clerk.pk
    assert event.data["previous"] == {"ministry": tree["moh"].pk, "preparer": prior_owner.pk}
    assert event.data["batch"] == batch.reference


def test_wizard_refuses_foreign_invoice_and_reuses_completed_batch(client, tree, people, provider, make_invoice):
    invoice = make_invoice()
    sign_in(client, people["clerk"])
    response = client.post(reverse("operations:prepare_new"), {
        "name": "Scoped", "org_unit": tree["moh"].pk, "provider": provider.pk, "currency": "MUR",
    })
    prep = BatchPreparation.objects.get(prepared_by=people["clerk"])
    response = client.post(f"{prep.get_absolute_url()}?step=select", {
        "action": "select", "invoices": [invoice.pk + 99999],
    })
    assert response.status_code == 200
    assert b"Select only available invoices" in response.content
    prep.refresh_from_db()
    assert prep.completed_batch_id is None


def test_individual_invoice_action_starts_wizard_with_saved_template_review_path(
        client, tree, people, make_invoice):
    invoice = make_invoice()
    sign_in(client, people["clerk"])
    page = client.get(invoice.get_absolute_url())
    assert b"Prepare a batch with this invoice" in page.content
    assert b"Approval is performed on the complete batch" in page.content
    setup = client.get(reverse("operations:prepare_new"), {"invoice": invoice.pk})
    assert setup.status_code == 200
    assert setup.context["source_invoice"] == invoice
    assert setup.context["form"].initial["provider"] == invoice.provider_id
    assert setup.context["form"].initial["org_unit"] == tree["moh"].pk


def test_approval_inbox_groups_members_under_one_batch(client, tree, people, provider, make_invoice, policy):
    first, second = make_invoice(), make_invoice()
    batch = ops.create_batch(people["clerk"], tree["moh"], "Two utility invoices",
                             [first.pk, second.pk])
    # The real reconciliation gate remains in force during submission.
    from tests.test_reconciliation import upload
    from reconciliation.services import assign, recheck
    lines = "".join(
        f"{invoice.service_account.account_number},{invoice.invoice_number},"
        f"{invoice.amount_total},{invoice.currency}\n" for invoice in (first, second))
    statement = upload(people["finance"], first, lines)
    for invoice in (first, second):
        row = statement.rows.get(invoice_number=invoice.invoice_number)
        assign(row, people["finance"], tree["hosp"], people["clerk"], "Matched to ministry account")
        recheck(row, people["finance"])
    start_batch(batch, people["clerk"])
    sign_in(client, people["verifier"])

    response = client.get(reverse("workflow:inbox"))
    assert response.status_code == 200
    assert len(response.context["batch_rows"]) == 1
    assert response.context["batch_rows"][0]["invoice_count"] == 2
    assert first.reference.encode() not in response.content
    assert response.context["batch_rows"][0]["batch"] == batch
    detail = client.get(reverse("workflow:batch_review", args=[batch.pk]))
    assert detail.status_code == 200
    assert first.reference.encode() in detail.content and second.reference.encode() in detail.content
    task = first.approval_tasks.get(status="pending")
    individual = client.get(reverse("workflow:task", args=[task.pk]))
    assert individual.status_code == 302
    assert individual.url == reverse("workflow:batch_review", args=[batch.pk])
