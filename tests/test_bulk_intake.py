import csv
import io
import stat
import zipfile
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import OTP_SESSION_KEY
from imports.bulk import apply_folder_paths, expand_uploads, stage_uploads
from imports.models import ImportBatch
from imports.services import commit
from invoices.intake_dates import assess_dates
from invoices.models import Invoice


def csv_file(number="BULK-1", *, received=None, due=None):
    today = timezone.localdate()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Account No", "Invoice No", "Issue Date", "Due Date", "Total Due", "Receipt Date"])
    writer.writerow(["ACC-1", number, (today - timedelta(days=10)).isoformat(),
                     (due or today + timedelta(days=30)).isoformat(), "100", (received or today).isoformat()])
    return SimpleUploadedFile(f"{number}.csv", output.getvalue().encode(), content_type="text/csv")


def zip_file(entries, name="invoices.zip"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries:
            archive.writestr(path, content)
    return SimpleUploadedFile(name, output.getvalue())


@pytest.mark.django_db
def test_nested_zip_and_mixed_sheets_keep_receipt_dates(people, account, provider):
    today = timezone.localdate()
    received = today - timedelta(days=8)
    first = csv_file("ZIP-1", received=received)
    archive = zip_file([("department/month/ZIP-1.csv", first.read()), ("notes.txt", b"not an invoice")])
    batches, skipped = stage_uploads(people["clerk"], [archive, csv_file("MIXED-2")], provider)
    assert len(batches) == 2 and len(skipped) == 1
    row = batches[0].rows.get()
    assert row.data["received_date"] == received.isoformat()
    assert any("after receipt" in note for note in row.data["date_anomalies"])
    assert batches[0].original_name == "invoices.zip/department/month/ZIP-1.csv"
    created, failed = commit(batches[0], people["clerk"])
    assert failed == 0 and len(created) == 1 and created[0].received_date == received
    assert "recording_delay" in created[0].flags
    assert "notes.txt" in batches[0].intake_notes[0]


@pytest.mark.parametrize("name", ["../secret.pdf", "/etc/invoice.pdf", "C:/invoice.pdf", "nested\\invoice.pdf"])
def test_zip_rejects_unsafe_paths(name):
    with pytest.raises(ValidationError, match="Unsafe"):
        expand_uploads([zip_file([(name, b"%PDF-")])])


def test_zip_rejects_symlinks():
    info = zipfile.ZipInfo("link.pdf")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValidationError, match="symbolic"):
        expand_uploads([zip_file([(info, b"/etc/passwd")])])


def test_zip_limits_decompressed_size(settings):
    settings.MAX_UPLOAD_MB = 1
    with pytest.raises(ValidationError, match="per-file"):
        expand_uploads([zip_file([("large.pdf", b"%PDF-" + b"0" * (2 * 1024 * 1024))])])


def test_folder_paths_preserve_subfolders_and_reject_mismatch():
    file = csv_file()
    apply_folder_paths([file], '["Health/October/BULK-1.csv"]')
    assert file.intake_path == "Health/October/BULK-1.csv"
    with pytest.raises(ValidationError):
        apply_folder_paths([file], '["Health/wrong.csv"]')


@pytest.mark.django_db
def test_corrupt_invoice_does_not_create_partial_batches(people, provider):
    forged = SimpleUploadedFile("invoice.pdf", b"this is not a PDF")
    with pytest.raises(ValidationError):
        stage_uploads(people["clerk"], [csv_file(), forged], provider)
    assert ImportBatch.objects.count() == 0


@pytest.mark.django_db
def test_cross_unit_files_cannot_create_payable_invoices(people, provider, account):
    batches, _ = stage_uploads(people["outsider"], [csv_file()], provider)
    assert batches[0].rows.get().status == "invalid"
    assert commit(batches[0], people["outsider"])[0] == []
    assert not Invoice.objects.exists()


@pytest.mark.django_db
def test_folder_post_and_priority_screen(client, people, provider, account):
    client.force_login(people["clerk"])
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()
    upload = client.post(reverse("imports:list"), {
        "provider": provider.pk, "received_date": timezone.localdate().isoformat(),
        "folder_files": [csv_file(due=timezone.localdate() - timedelta(days=1))],
        "folder_paths": '["Invoices/BULK-1.csv"]',
    })
    assert upload.status_code == 302
    batch = ImportBatch.objects.get()
    detail = client.get(upload.url)
    assert detail.status_code == 200 and b"Received" in detail.content and b"Overdue" in detail.content
    assert b"Record invoice" not in client.get(reverse("imports:list")).content
    commit(batch, people["clerk"])
    urgent = client.get(reverse("invoices:list"), {"priority": "urgent"})
    assert urgent.status_code == 200 and b"BULK-1" in urgent.content
    normal = client.get(reverse("invoices:list"), {"priority": "normal"})
    assert b"BULK-1" not in normal.content


@pytest.mark.django_db
@pytest.mark.parametrize("received_offset,issue_offset,due_offset,expected", [
    (2, -10, 30, "future_receipt"),
    (-10, -2, 30, "receipt_before_issue"),
    (0, -10, -1, "received_overdue"),
    (-9, -10, 30, "recording_delay"),
])
def test_receipt_date_anomalies(received_offset, issue_offset, due_offset, expected):
    today = timezone.localdate()
    result = assess_dates(today + timedelta(days=issue_offset), today + timedelta(days=due_offset),
                          today + timedelta(days=received_offset), today)
    assert expected in result["flags"]


@pytest.mark.django_db
def test_priority_uses_approval_deadline_not_only_payment_date():
    today = timezone.localdate()
    assert assess_dates(today, today - timedelta(days=1), today)["priority"] == "overdue"
    assert assess_dates(today, today, today)["priority"] == "urgent"
    assert assess_dates(today, today + timedelta(days=6), today)["priority"] == "high"
    assert assess_dates(today, today + timedelta(days=30), today)["priority"] == "normal"
