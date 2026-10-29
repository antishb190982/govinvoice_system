"""Read-only, derived approval copies. Never rewrite source or signed files."""

import hashlib
import io
from datetime import datetime
from xml.sax.saxutils import escape

from django.core.exceptions import ValidationError
from django.db.models import F
from django.utils import timezone
from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from invoices.models import Invoice
from workflow.models import ApprovalTask


def approval_evidence(invoice):
    if not invoice.approved_at or invoice.status not in {
        Invoice.Status.APPROVED, Invoice.Status.SCHEDULED, Invoice.Status.PAID, Invoice.Status.DISPUTED,
    }:
        return None
    tasks = list(invoice.approval_tasks.select_related("decided_by").order_by("-round", "order"))
    latest_round = tasks[0].round if tasks else None
    decisions = [
        {"name": t.name, "role": t.get_role_display(),
         "person": str(t.decided_by) if t.decided_by else "Not recorded", "at": t.decided_at}
        for t in tasks if t.round == latest_round and t.status == ApprovalTask.Status.APPROVED
    ]
    # Canceled instructions must never supply a bank reference or override
    # the batch used by the live instruction.
    item = invoice.settlement_items.exclude(line__instruction__status="cancelled").select_related(
        "line__instruction__batch", "line__instruction__prepared_by").order_by(
            F("line__instruction__executed_at").desc(nulls_last=True), "-line__instruction__prepared_at").first()
    instruction = item.line.instruction if item else None
    member = invoice.batch_memberships.filter(active=True).select_related("batch").first()
    batch = instruction.batch if instruction and instruction.batch_id else member.batch if member else None
    executed = bool(instruction and instruction.status == "executed")
    return {
        "approved_at": invoice.approved_at, "recorded_by": str(invoice.created_by),
        "recorded_at": invoice.created_at, "received_date": invoice.received_date,
        "submitted_at": invoice.submitted_at, "decisions": decisions, "round": latest_round,
        "batch_reference": batch.reference if batch else "Not assigned",
        "processing_started_at": batch.started_at if batch else None,
        "settlement_reference": instruction.reference if instruction else "Not prepared",
        "settlement_status": instruction.get_status_display() if instruction else "Pending settlement",
        "finance_prepared_by": str(instruction.prepared_by) if instruction else "Not recorded",
        "finance_prepared_at": instruction.prepared_at if instruction else None,
        "bank_reference": (instruction.bank_reference if executed else "") or (
            invoice.payment_reference if invoice.paid_at else "") or "Pending bank settlement",
        "settled_at": instruction.executed_at if executed else invoice.paid_at,
        "generated_at": timezone.now(), "source_sha256": invoice.document_sha256 or "Not recorded",
    }


def display_date(value):
    if not value:
        return "Not recorded"
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%d %b %Y %H:%M %Z")
    return value.strftime("%d %b %Y")


def _record_rows(invoice, evidence):
    e = evidence
    rows = [
        ("Invoice", f"{invoice.reference} / {invoice.invoice_number}"),
        ("Current status", invoice.get_status_display()),
        ("Provider", str(invoice.provider)),
        ("Service account", invoice.service_account.account_number),
        ("Billed total", f"{invoice.currency} {invoice.amount_total}"),
        ("Due date", display_date(invoice.due_date)),
        ("Recorded by / date", f"{e['recorded_by']} / {display_date(e['recorded_at'])}"),
        ("Receipt date", display_date(e["received_date"])),
        ("Submitted", display_date(e["submitted_at"])),
    ]
    for d in e["decisions"]:
        rows.append((f"{d['name']} ({d['role']})", f"{d['person']} / {display_date(d['at'])}"))
    if not e["decisions"]:
        rows.append(("Review / approval actors", "Not recorded in the available workflow history"))
    rows += [
        ("Approved", display_date(e["approved_at"])),
        ("Batch number", e["batch_reference"]),
        ("Batch processing started", display_date(e["processing_started_at"])),
        ("Settlement instruction", f"{e['settlement_reference']} / {e['settlement_status']}"),
        ("Finance processing", f"{e['finance_prepared_by']} / {display_date(e['finance_prepared_at'])}"),
        ("Bank settlement reference", e["bank_reference"]),
        ("Settled on", display_date(e["settled_at"])),
        ("Source SHA-256", e["source_sha256"]),
        ("Copy generated", display_date(e["generated_at"])),
    ]
    return rows


def _record_pdf(invoice, evidence):
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    story = [Paragraph("GovBills - Invoice approval record", styles["Title"])]
    story.append(Paragraph(
        "Derived operational copy, not a new digital signature. The original invoice and signed "
        "approval/settlement evidence remain unchanged. Provider acceptance is not bank execution.",
        styles["Normal"]))
    if not invoice.document:
        story.append(Paragraph("No original invoice document is attached to this record.", styles["Normal"]))
    for label, value in _record_rows(invoice, evidence):
        story += [Spacer(1, 7), Paragraph(f"<b>{escape(label)}</b>: {escape(str(value))}", styles["Normal"])]
    SimpleDocTemplate(output, pagesize=A4, leftMargin=36, rightMargin=36,
                      topMargin=36, bottomMargin=36).build(story)
    return output.getvalue()


def _stamp(invoice, evidence, page_number, total):
    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=A4)
    w, h = A4
    c.setFillColor(colors.HexColor("#124d59"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(30, h - 24, f"GOVBILLS - APPROVAL COPY / {invoice.reference}")
    lines = [
        f"Approved: {display_date(evidence['approved_at'])} | Current status: {invoice.get_status_display()}",
    ]
    for d in evidence["decisions"][:4]:
        lines.append(f"{d['name']}: {d['person']} / {display_date(d['at'])}")
    if not evidence["decisions"]:
        lines.append("Reviewer / approver: not recorded in available workflow history")
    lines += [
        f"Batch: {evidence['batch_reference']} | Processing: {display_date(evidence['processing_started_at'])}",
        f"Bank settlement reference: {evidence['bank_reference']}",
        f"Settlement: {evidence['settlement_reference']} / {evidence['settlement_status']}",
        f"Settled: {display_date(evidence['settled_at'])}",
        f"Original page {page_number} of {total}. Full approval record follows the source pages.",
        "Derived copy only. Original files and digital signatures have not been changed.",
    ]
    # Paragraph wraps long names/references rather than clipping audit evidence.
    style = getSampleStyleSheet()["Normal"]
    style.fontSize, style.leading = 8, 10
    p = Paragraph("<br/>".join(escape(line) for line in lines), style)
    _, height = p.wrap(w - 60, 200)
    p.drawOn(c, 30, 20)
    c.save()
    return output.getvalue(), max(165, height + 35)


def render_approved_copy(invoice, evidence):
    """Return a PDF containing watermarked source pages followed by full history."""
    record = _record_pdf(invoice, evidence)
    writer = PdfWriter()
    if invoice.document:
        with invoice.document.open("rb") as source:
            data = source.read()
        digest = hashlib.sha256(data).hexdigest()
        if invoice.document_sha256 and digest != invoice.document_sha256:
            raise ValidationError("The source document no longer matches its recorded SHA-256. Contact an administrator.")
        evidence["source_sha256"] = digest
        record = _record_pdf(invoice, evidence)
        try:
            if data.startswith(b"%PDF"):
                original = PdfReader(io.BytesIO(data))
                if original.is_encrypted and not original.decrypt(""):
                    raise ValueError("Encrypted source")
                pages = list(original.pages)
            else:
                # Uploaded PNG/JPEGs also produce a downloadable approved PDF.
                image = ImageReader(io.BytesIO(data))
                width, height = image.getSize()
                image_pdf = io.BytesIO()
                c = canvas.Canvas(image_pdf, pagesize=(width, height))
                c.drawImage(image, 0, 0, width=width, height=height)
                c.save()
                pages = list(PdfReader(io.BytesIO(image_pdf.getvalue())).pages)
            if not pages:
                raise ValueError("Empty document")
            for index, page in enumerate(pages, 1):
                page.transfer_rotation_to_content()
                stamp_data, footer = _stamp(invoice, evidence, index, len(pages))
                w, h = A4
                target = writer.add_blank_page(width=w, height=h)
                box = page.cropbox
                scale = min((w - 60) / float(box.width), (h - footer - 45) / float(box.height))
                tx = (w - float(box.width) * scale) / 2
                ty = footer + (h - footer - 45 - float(box.height) * scale) / 2
                target.merge_transformed_page(page, Transformation().translate(
                    -float(box.left), -float(box.bottom)).scale(scale).translate(tx, ty))
                target.merge_page(PdfReader(io.BytesIO(stamp_data)).pages[0])
        except Exception as exc:
            raise ValidationError("The original document could not be rendered safely. "
                                  "Use the approval record or download the original separately.") from exc
    for page in PdfReader(io.BytesIO(record)).pages:
        writer.add_page(page)
    writer.add_metadata({"/Title": f"{invoice.reference} - Approved invoice copy",
                         "/Subject": "Derived approval record; original signatures remain on original evidence"})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
