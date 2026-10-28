"""OCR on synthetic scans: field location, provider identification, confidence gate, template learning."""

import shutil
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from imports import services as imports
from imports.models import ImportRow
from invoices.models import Invoice
from ocr import services as ocr
from ocr.models import OcrTemplate
from providers.models import Provider, ServiceAccount, UtilityType
from ocr.samples import as_bytes, as_scanned_pdf, invoice_image
from tests.test_views import sign_in

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract not installed")


@pytest.fixture
def ceb(tree):
    p = Provider.objects.create(name="Central Electricity Board", short_code="CEB", utility_type=UtilityType.ELECTRICITY,
                                bank_iban="MU31MCBL0901000000440011000MUR", bank_account_name="CEB", currency="MUR",
                                identifiers="CEB\nCentral Electricity Board (Mauritius)")
    ServiceAccount.objects.create(provider=p, org_unit=tree["hosp"], account_number="1100453201", premises="Main block")
    Provider.objects.create(name="Central Water Authority", short_code="CWA", utility_type=UtilityType.WATER, currency="MUR")
    return p


def _dates():
    today = timezone.localdate()
    return dict(issue=today - timedelta(days=5), due=today + timedelta(days=40),
                start=(today - timedelta(days=40)).replace(day=1), end=today - timedelta(days=8))


def _upload(img, name="scan.png", fmt="PNG"):
    content = as_scanned_pdf(img) if name.endswith(".pdf") else as_bytes(img, fmt)
    return SimpleUploadedFile(name, content)


def test_scanned_photo_fields_located_and_provider_identified(people, ceb):
    d = _dates()
    img = invoice_image(provider_name="Central Electricity Board", account="1100453201", number="CEB/2609/0301",
                        skew=1.5, noise=0.002, blur=0.6, **d)
    batch = imports.stage_documents(people["clerk"], [_upload(img, "photo.jpg", "JPEG")])  # no provider chosen
    row = batch.rows.get()
    doc = row.ocr_document
    assert doc.used_ocr and doc.provider == ceb, doc.extraction.get("provider")
    ex = doc.extraction
    assert ex["account_number"]["value"] == "1100453201" and ex["account_number"]["source"] == "registry"
    assert ex["invoice_number"]["value"] == "CEB/2609/0301"
    assert ex["amount_total"]["value"] == "143750.000"
    assert ex["amount_tax"]["value"] == "18750.000"  # not the "Amount before tax" line
    assert ex["due_date"]["value"] == d["due"].isoformat()
    for field in ("account_number", "amount_total", "due_date"):
        x0, y0, x1, y1 = ex[field]["bbox"]
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1  # located on the page
    assert row.status == ImportRow.Status.VALID, row.errors


def test_rotated_scanned_pdf_is_corrected(people, ceb):
    d = _dates()
    img = invoice_image(provider_name="Central Electricity Board", account="1100453201", number="CEB/2609/0302",
                        rotate90=True, **d)
    batch = imports.stage_documents(people["clerk"], [_upload(img, "scan.pdf")], provider=ceb)
    doc = batch.rows.get().ocr_document
    assert doc.pages[0]["method"] == "ocr" and doc.pages[0]["rotation"] in (90, 270)
    assert doc.extraction["amount_total"]["value"] == "143750.000"


def test_low_confidence_requires_confirmation_then_learns_template(client, people, ceb):
    d = _dates()
    img = invoice_image(provider_name="Central Electricity Board", account="1100453201", number="CEB/2609/0303",
                        layout="stacked", **d)
    batch = imports.stage_documents(people["clerk"], [_upload(img)], provider=ceb)
    row = batch.rows.get()
    row.needs_review = True  # force the gate regardless of how confident this scan was
    row.save()
    created, _ = imports.commit(batch, people["clerk"])
    assert created == [] and batch.status == "staged"  # unconfirmed rows are held back

    sign_in(client, people["clerk"])
    page = client.get(reverse("ocr:document", args=[row.ocr_document_id]))
    assert page.status_code == 200 and b"Where each value was found" in page.content
    values = {k: (v or "") for k, v in row.raw.items()}
    r = client.post(reverse("ocr:document", args=[row.ocr_document_id]), {**values, "learn": "on"})
    assert r.status_code == 302
    template = OcrTemplate.objects.get(provider=ceb)
    assert {"amount_total", "invoice_number", "due_date"} <= set(template.zones)
    assert template.zones["amount_total"].get("anchor")  # anchored to its printed label
    created, _ = imports.commit(batch, people["clerk"])
    inv = Invoice.objects.get(invoice_number="CEB/2609/0303")
    assert inv.source == Invoice.Source.OCR and inv.ocr_document_id == row.ocr_document_id

    # Next invoice from the same provider, shifted on the page, is read through the template.
    img2 = invoice_image(provider_name="Central Electricity Board", account="1100453201", number="CEB/2610/0303",
                         layout="stacked", skew=0.8, total="99,000.00", net="86,086.96", vat="12,913.04", **d)
    batch2 = imports.stage_documents(people["clerk"], [_upload(img2, "next.png")], provider=ceb)
    ex = batch2.rows.get().ocr_document.extraction
    assert ex["amount_total"]["value"] == "99000.000"
    assert ex["amount_total"]["source"] in ("template", "label")
    assert ex["invoice_number"]["value"] == "CEB/2610/0303"


def test_documents_are_private(client, people, ceb):
    d = _dates()
    batch = imports.stage_documents(people["clerk"], [_upload(invoice_image(account="1100453201", **d))], provider=ceb)
    doc = batch.rows.get().ocr_document
    sign_in(client, people["outsider"])
    assert client.get(reverse("ocr:document", args=[doc.pk])).status_code == 404
    assert client.get(reverse("ocr:preview", args=[doc.pk, 0])).status_code == 404


def test_template_editor_saves_anchored_zones(client, tree, ceb, people):
    from tests.conftest import make_user
    from org.models import Role

    d = _dates()
    batch = imports.stage_documents(people["clerk"], [_upload(invoice_image(account="1100453201", **d))], provider=ceb)
    doc = batch.rows.get().ocr_document
    admin = make_user("gadmin", [(Role.UNIT_ADMIN, tree["gov"])])
    sign_in(client, admin)
    total_box = doc.extraction["amount_total"]["bbox"]
    import json

    r = client.post(reverse("ocr:template_edit", args=[ceb.pk]) + f"?doc={doc.pk}",
                    {"zones": json.dumps({"amount_total": {"page": 0, "bbox": total_box}}), "is_active": "on"})
    assert r.status_code == 302
    zone = OcrTemplate.objects.get(provider=ceb).zones["amount_total"]
    assert zone["anchor"] in ("total amount due", "amount due")
    assert client.get(reverse("ocr:templates")).status_code == 200


def test_unreadable_image_is_rejected_cleanly(people, ceb):
    from PIL import Image

    blank = Image.new("RGB", (1200, 1600), "white")
    batch = imports.stage_documents(people["clerk"], [_upload(blank, "blank.png")], provider=ceb)
    row = batch.rows.get()
    assert row.status == ImportRow.Status.INVALID and row.errors


def test_learn_locates_confirmed_values(people, ceb):
    d = _dates()
    content = as_bytes(invoice_image(account="1100453201", **d))
    doc = ocr.create_document(people["clerk"], content, "x.png", "png")
    ocr.analyse(doc, hint=ceb)
    where = ocr.locate(doc, "amount_total", "143750.00")
    assert where and where[2]  # page, bbox, anchor label
