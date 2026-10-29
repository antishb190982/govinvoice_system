import hashlib
import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from pypdf import PdfReader

from signatures import services
from signatures.appearance import normalise_image, signature_appearance
from tests.conftest import PIN
from tests.test_invoice_evidence_gateways import approve, sign_in
from invoices.evidence import approval_evidence, render_approved_copy
from org.models import Role
from workflow.engine import Decision, decision_payload

pytestmark = pytest.mark.django_db


def image_file(color="navy"):
    output = io.BytesIO()
    Image.new("RGB", (240, 75), color).save(output, "PNG")
    return SimpleUploadedFile("signature.png", output.getvalue(), content_type="image/png")


def upload(client, user, image=None, **extra):
    sign_in(client, user)
    return client.post(reverse("signatures:appearance"), {
        "pin": PIN, "confirm": "on", "image": image or image_file(), **extra,
    })


def image_approval(invoice, signers):
    invoice = approve(invoice, signers)
    invoice.refresh_from_db()
    task = invoice.approval_tasks.get(role=Role.APPROVER)
    signature = services.sign_payload(signers["approver"], PIN,
        decision_payload(task, Decision.APPROVE, task.comment, task.decided_at), "approval-stamp-test")
    signature.approval_task = task
    signature.save()
    return invoice, signature


def test_approval_stamp_contains_batch_name_actor_time_and_verified_image(client, make_invoice, signers):
    from operations.services import create_batch
    assert upload(client, signers["approver"]).status_code == 302
    invoice = make_invoice()
    batch = create_batch(signers["clerk"], invoice.org_unit, "CEB stamp verification batch", [invoice.pk])
    invoice, signature = image_approval(invoice, signers)
    before_hash = invoice.document_sha256
    data = render_approved_copy(invoice, approval_evidence(invoice))
    pdf = PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() for page in pdf.pages)
    for expected in ["APPROVAL STAMP", batch.name, batch.reference, "Approver",
                     "User approved / date and time", "Pending bank settlement"]:
        assert expected in text
    assert len(pdf.pages[0].images) >= 1
    assert float(pdf.pages[0].mediabox.height) > 841
    assert signature_appearance(signature)[0]
    invoice.refresh_from_db()
    assert invoice.document_sha256 == before_hash
    sign_in(client, signers["approver"])
    response = client.get(reverse("invoices:approved_copy", args=[invoice.pk]))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]


def test_signature_image_change_and_removal_do_not_rewrite_historical_signature(client, make_invoice, signers):
    upload(client, signers["approver"])
    invoice, signature = image_approval(make_invoice(), signers)
    original_image, _ = signature_appearance(signature)
    original_payload = signature.payload_sha256
    upload(client, signers["approver"], image_file("green"))
    assert signature_appearance(signature)[0] == original_image
    assert upload(client, signers["approver"], action="remove").status_code == 302
    signature.refresh_from_db()
    assert signature.payload_sha256 == original_payload
    assert signature_appearance(signature)[0] == original_image
    assert render_approved_copy(invoice, approval_evidence(invoice))


def test_tampered_signature_image_cannot_be_used_in_stamp(client, make_invoice, signers):
    upload(client, signers["approver"])
    invoice, signature = image_approval(make_invoice(), signers)
    with signature.appearance_image.storage.open(signature.appearance_image.name, "wb") as target:
        target.write(b"tampered")
    with pytest.raises(ValidationError, match="SHA-256"):
        render_approved_copy(invoice, approval_evidence(invoice))


def test_signature_image_requires_valid_pin_and_rejects_non_images(client, signers):
    response = upload(client, signers["approver"], pin="incorrect-pin")
    assert response.status_code == 200
    assert not services.active_identity(signers["approver"]).signature_image
    response = upload(client, signers["approver"],
                      SimpleUploadedFile("bad.svg", b"<svg>fake</svg>", content_type="image/svg+xml"))
    assert response.status_code == 200
    assert not services.active_identity(signers["approver"]).signature_image


def test_signature_image_preview_is_private_and_per_user(client, signers):
    upload(client, signers["approver"])
    response = client.get(reverse("signatures:appearance"))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    client.logout()
    sign_in(client, signers["approver2"])
    assert client.get(reverse("signatures:appearance")).status_code == 404
    client.logout()
    assert client.get(reverse("signatures:appearance")).status_code == 302


def test_normalised_signature_digest_and_image_type():
    data, digest = normalise_image(image_file())
    assert data.startswith(b"\x89PNG")
    assert hashlib.sha256(data).hexdigest() == digest
