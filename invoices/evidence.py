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
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, Image

from invoices.models import Invoice
from workflow.models import ApprovalTask


def approval_evidence(invoice):
    if not invoice.approved_at or invoice.status not in {
        Invoice.Status.APPROVED, Invoice.Status.SCHEDULED, Invoice.Status.PAID, Invoice.Status.DISPUTED,
    }:
        return None
    tasks = list(invoice.approval_tasks.select_related("decided_by", "signature__identity").order_by("-round", "order"))
    latest_round = tasks[0].round if tasks else None
    decisions = [
        {"name": t.name, "role": t.get_role_display(),
         "person": str(t.decided_by) if t.decided_by else "Not recorded", "at": t.decided_at,
         "signature": getattr(t, "signature", None), "task_id": t.pk,
         "person_id": t.decided_by_id,
         "signature_image_recorded": bool(getattr(t, "signature", None) and t.signature.appearance_image)}
        for t in tasks if t.round == latest_round and t.status == ApprovalTask.Status.APPROVED
    ]
    # Canceled instructions must never supply a bank reference or override
    # the batch used by the live instruction.
    item = invoice.settlement_items.exclude(line__instruction__status="cancelled").select_related(
        "batch", "line__instruction__batch", "line__instruction__prepared_by").order_by(
            F("line__instruction__executed_at").desc(nulls_last=True), "-line__instruction__prepared_at").first()
    instruction = item.line.instruction if item else None
    member = invoice.batch_memberships.filter(active=True).select_related("batch").first()
    batch = item.batch if item and item.batch_id else (
        instruction.batch if instruction and instruction.batch_id else member.batch if member else None)
    executed = bool(instruction and instruction.status == "executed")
    return {
        "approved_at": invoice.approved_at, "recorded_by": str(invoice.created_by),
        "demonstration": invoice.org_unit.code == "STAMP-DEMO",
        "recorded_at": invoice.created_at, "received_date": invoice.received_date,
        "submitted_at": invoice.submitted_at, "decisions": decisions, "round": latest_round,
        "batch_reference": batch.reference if batch else "Not assigned",
        "batch_name": batch.name if batch else "Not assigned",
        "processing_started_at": batch.started_at if batch else None,
        "settlement_reference": instruction.reference if instruction else "Not prepared",
        "settlement_group": instruction.group_reference if instruction else None,
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
        ("Batch name", e["batch_name"]),
        ("Batch processing started", display_date(e["processing_started_at"])),
        ("Settlement instruction", f"{e['settlement_reference']} / {e['settlement_status']}"),
        ("Finance processing", f"{e['finance_prepared_by']} / {display_date(e['finance_prepared_at'])}"),
        ("Bank settlement reference", e["bank_reference"]),
        ("Settled on", display_date(e["settled_at"])),
        ("Source SHA-256", e["source_sha256"]),
        ("Copy generated", display_date(e["generated_at"])),
    ]
    if e.get("settlement_group"):
        rows.insert(-1, ("Settlement group", e["settlement_group"]))
    return rows


def _record_pdf(invoice, evidence):
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    story = [Paragraph("GovBills - Invoice approval record", styles["Title"])]
    story.append(Paragraph(
        "Derived operational copy, not a new digital signature. The original invoice and signed "
        "approval/settlement evidence remain unchanged. Provider acceptance is not bank execution.",
        styles["Normal"]))
    if evidence.get("demonstration"):
        story.append(Paragraph("DEMONSTRATION ONLY: The stamp is not a real organisational approval "
                               "or proof of payment, even when the source is a user-supplied bill.", styles["Normal"]))
    story += [Spacer(1, 12), _approval_table(invoice, evidence), Spacer(1, 12)]
    if not invoice.document:
        story.append(Paragraph("No original invoice document is attached to this record.", styles["Normal"]))
    for label, value in _record_rows(invoice, evidence):
        story += [Spacer(1, 7), Paragraph(f"<b>{escape(label)}</b>: {escape(str(value))}", styles["Normal"])]
    SimpleDocTemplate(output, pagesize=A4, leftMargin=36, rightMargin=36,
                      topMargin=36, bottomMargin=36).build(story)
    return output.getvalue()


def _approval_table(invoice, evidence, limit=None):
    """Boxed stamp; images come only from each decision's signed snapshot."""
    from signatures.appearance import signature_appearance

    style = getSampleStyleSheet()["Normal"]
    style.fontSize, style.leading = 7.5, 10

    def text(value):
        return Paragraph(escape(str(value)), style)

    rows = [
        [text("GOVBILLS - DEMONSTRATION APPROVAL STAMP - NOT A REAL APPROVAL OR PAYMENT"
              if evidence.get("demonstration") else "GOVBILLS - APPROVAL STAMP"), "", ""],
        [text(f"Batch: {evidence['batch_reference']} | {evidence['batch_name']}"), "", ""],
        [text(f"Approved: {display_date(evidence['approved_at'])} | Status: {invoice.get_status_display()}"), "", ""],
        [text("Step / responsibility"), text("User approved / date and time"), text("Recorded signature image")],
    ]
    decisions = evidence["decisions"][:limit] if limit else evidence["decisions"]
    for decision in decisions:
        signature = decision.get("signature")
        if "_image_bytes" not in decision:
            if signature:
                if (signature.identity.user_id != decision["person_id"]
                        or signature.payload.get("task", {}).get("id") != decision["task_id"]
                        or signature.payload.get("decision") != "approve"):
                    raise ValidationError("The approval signature does not match its recorded approver and task.")
                decision["_image_bytes"], decision["_image_status"] = signature_appearance(signature)
            else:
                decision["_image_bytes"], decision["_image_status"] = None, "No signed image recorded"
        data = decision["_image_bytes"]
        if data:
            width, height = ImageReader(io.BytesIO(data)).getSize()
            scale = min(112 / width, 36 / height)
            picture = Image(io.BytesIO(data), width=width * scale, height=height * scale)
        else:
            picture = text(decision["_image_status"])
        rows.append([text(f"{decision['name']} ({decision['role']})"),
                     text(f"{decision['person']} | {display_date(decision['at'])}"), picture])
    if not decisions:
        rows.append([text("Reviewer / approver not recorded in available workflow history"), "", ""])
    if limit and len(evidence["decisions"]) > limit:
        rows.append([text("Additional decisions and signature images appear in the full approval record."), "", ""])
    metadata_start = len(rows)
    for line in [
        f"Processing started: {display_date(evidence['processing_started_at'])}",
        f"Bank settlement reference: {evidence['bank_reference']}",
        f"Settlement: {evidence['settlement_reference']} / {evidence['settlement_status']}",
        f"Settled: {display_date(evidence['settled_at'])}",
        "Visual stamp on a derived copy - not a new digital signature. Original evidence is unchanged.",
    ]:
        rows.append([text(line), "", ""])
    table = Table(rows, colWidths=[125, 268, 130], hAlign="LEFT")
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#f5f8ff")),
        ("BOX", (0, 0), (-1, -1), 1.2, colors.HexColor("#2455a4")),
        ("LINEBELOW", (0, 3), (-1, 3), .5, colors.HexColor("#9eb9df")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for index in [0, 1, 2, *range(metadata_start, len(rows))]:
        commands.append(("SPAN", (0, index), (-1, index)))
    if not decisions:
        commands.append(("SPAN", (0, 4), (-1, 4)))
    if limit and len(evidence["decisions"]) > limit:
        commands.append(("SPAN", (0, metadata_start - 1), (-1, metadata_start - 1)))
    table.setStyle(TableStyle(commands))
    return table


def _stamp(invoice, evidence, page_number, total):
    output = io.BytesIO()
    table = _approval_table(invoice, evidence, limit=4)
    w, h = A4
    _, height = table.wrap(w - 72, h)
    footer = height + 48
    # Add a dedicated stamp area below the source, instead of placing the
    # signature or stamp on top of provider amounts, account numbers or barcodes.
    c = canvas.Canvas(output, pagesize=(w, h + footer))
    c.setFillColor(colors.HexColor("#124d59"))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(36, h + footer - 24, f"GOVBILLS - APPROVAL COPY / {invoice.reference}")
    table.drawOn(c, 36, 32)
    c.setFont("Helvetica", 7)
    c.drawString(36, 18, f"Original page {page_number} of {total}. Full approval record follows the source pages.")
    c.save()
    return output.getvalue(), footer


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
                target = writer.add_blank_page(width=w, height=h + footer)
                box = page.cropbox
                scale = min((w - 60) / float(box.width), (h - 45) / float(box.height))
                tx = (w - float(box.width) * scale) / 2
                ty = footer + (h - 45 - float(box.height) * scale) / 2
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
