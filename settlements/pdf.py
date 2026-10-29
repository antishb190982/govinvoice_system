"""Settlement instruction PDF. The last page carries two signature boxes."""

import io

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PAGE_W, PAGE_H = A4
UNICODE_FONT = "GovBillsUnicode"


def _local_font():
    """Register a TTF with Arabic glyphs (PDF_UNICODE_FONT), or return None."""
    from pathlib import Path

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if UNICODE_FONT in pdfmetrics.getRegisteredFontNames():
        return UNICODE_FONT
    path = Path(settings.PDF_UNICODE_FONT)
    if not path.exists():
        return None
    pdfmetrics.registerFont(TTFont(UNICODE_FONT, str(path)))
    return UNICODE_FONT


def _local_text(text):
    """Shape Arabic letters and apply right-to-left order for PDF output."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError:
        return None
    return get_display(arabic_reshaper.reshape(text))
# Signature boxes on the final page, in PDF points (x1, y1, x2, y2).
SIGNATURE_BOXES = {
    "Prepared": (int(20 * mm), int(60 * mm), int(100 * mm), int(95 * mm)),
    "Authorised": (int(110 * mm), int(60 * mm), int(190 * mm), int(95 * mm)),
}

INK = colors.HexColor("#14213d")
MUTED = colors.HexColor("#5c677d")
LINE = colors.HexColor("#d5dae3")
HEAD = colors.HexColor("#e9edf4")


class SignatureBoxes(Flowable):
    """Draws the signature boxes at fixed page positions (pyHanko places the fields there)."""

    def wrap(self, avail_width, avail_height):
        return avail_width, 1

    def draw(self):
        canvas = self.canv
        ox, oy = canvas.absolutePosition(0, 0)
        canvas.saveState()
        for label, (x1, y1, x2, y2) in SIGNATURE_BOXES.items():
            canvas.setStrokeColor(LINE)
            canvas.rect(x1 - ox, y1 - oy, x2 - x1, y2 - y1)
            canvas.setFont("Helvetica-Bold", 8)
            canvas.setFillColor(INK)
            canvas.drawString(x1 - ox, y2 - oy + 3 * mm, f"{label} by")
        canvas.restoreState()


def _money(value, currency):
    from core.money import fmt

    return fmt(value, currency)


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontSize=16, textColor=INK, alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=base["Normal"], fontSize=9, textColor=MUTED),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=11, textColor=INK, spaceBefore=10, spaceAfter=4),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=8, leading=10),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9, leading=12),
    }


def _table(rows, widths, money_cols=(), total=False):
    t = Table(rows, colWidths=widths, repeatRows=1)
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for c in money_cols:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    if total:
        style += [("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8), ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK)]
    t.setStyle(TableStyle(style))
    return t


def render(instruction):
    s = _styles()
    buf = io.BytesIO()
    generated = timezone.localtime()

    def frame(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 10 * mm, f"{instruction.reference} · generated {generated:%d %b %Y %H:%M} · "
                                             "Restricted – contains payment details")
        canvas.drawRightString(PAGE_W - 20 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=f"Settlement instruction {instruction.reference}",
                            author=settings.SIGNING_ORGANISATION, subject="Utility settlement instruction")
    cur = instruction.currency
    lines = list(instruction.lines.select_related("provider").prefetch_related(
        "items__invoice__service_account", "items__invoice__org_unit"))
    ministry = instruction.org_unit

    font = _local_font()
    local = _local_text(ministry.name_local) if ministry.name_local and font else None
    story = [
        Paragraph("Utility Settlement Instruction", s["title"]),
        Paragraph(f"{ministry.name} ({ministry.code})"
                  + (f' · <font name="{font}">{local}</font>' if local else ""), s["sub"]),
        Spacer(1, 6 * mm),
        _table([
            ["Reference", instruction.reference, "Requested execution", f"{instruction.requested_execution_date:%d %b %Y}"],
            ["Currency", cur, "Must be paid by", f"{instruction.pay_by:%d %b %Y}"],
            ["Payer", instruction.payer_name, "Debit account", instruction.payer_iban or "—"],
            ["Invoices due up to", f"{instruction.cutoff_date:%d %b %Y}", "Beneficiaries", str(len(lines))],
        ], [32 * mm, 53 * mm, 35 * mm, 50 * mm]),
        Paragraph("Amounts to settle per utility provider", s["h2"]),
    ]
    if instruction.group_reference:
        from xml.sax.saxutils import escape
        story.append(Paragraph(f"Settlement group {escape(instruction.group_reference)}", s["h2"]))
        batch_rows = [["Processing batch", "Invoices", f"Payable ({cur})"]]
        for link in instruction.batch_links.select_related("batch"):
            batch_rows.append([link.batch.reference, str(link.invoice_count), _money(link.amount, cur)])
        story.append(_table(batch_rows, [100 * mm, 25 * mm, 45 * mm], money_cols=(2,)))
    rows = [["#", "Provider", "Utility", "Beneficiary account", "Invoices", "Earliest due", "Amount"]]
    for i, line in enumerate(lines, 1):
        rows.append([str(i), Paragraph(line.beneficiary_name, s["cell"]), line.provider.get_utility_type_display(),
                     Paragraph(f"{line.beneficiary_iban or '—'}<br/>{line.beneficiary_bic}", s["cell"]),
                     str(line.invoice_count), f"{line.earliest_due:%d %b %Y}", _money(line.amount, cur)])
    rows.append(["", "Total", "", "", str(sum(li.invoice_count for li in lines)), "",
                 _money(instruction.total_amount, cur)])
    story.append(_table(rows, [7 * mm, 38 * mm, 22 * mm, 40 * mm, 14 * mm, 21 * mm, 28 * mm],
                        money_cols=(4, 6), total=True))
    if instruction.notes:
        story += [Paragraph("Notes", s["h2"]), Paragraph(instruction.notes, s["body"])]

    story.append(Paragraph("Annex – invoices included", s["h2"]))
    rows = [["Provider", "Ref.", "Provider invoice", "Account / unit", "Period", "Due", "Amount"]]
    for line in lines:
        for item in line.items.all():
            inv = item.invoice
            period = f"{inv.period_start:%d/%m}–{inv.period_end:%d/%m/%y}" if inv.period_start and inv.period_end else ""
            rows.append([line.provider.short_code, inv.reference, inv.invoice_number,
                         Paragraph(f"{inv.service_account.account_number}<br/>{inv.org_unit.code}", s["cell"]),
                         period, f"{inv.due_date:%d %b %Y}", _money(item.amount, cur)])
    story.append(_table(rows, [18 * mm, 30 * mm, 28 * mm, 30 * mm, 22 * mm, 20 * mm, 22 * mm], money_cols=(6,)))

    story += [PageBreak(), Paragraph("Authorisation", s["title"]), Spacer(1, 4 * mm), Paragraph(
        "By signing, the preparer confirms that every included invoice completed its approval workflow and "
        "the beneficiary details match the provider master data. The authoriser instructs the paying bank or "
        "treasury to execute the transfers above on or before the stated date. Signatures are PAdES digital "
        "signatures; any change to this document after signing invalidates them.", s["body"])]


    story.append(SignatureBoxes())
    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    return buf.getvalue()


def page_count(pdf_bytes):
    from pyhanko.pdf_utils.reader import PdfFileReader

    return int(PdfFileReader(io.BytesIO(pdf_bytes), strict=False).root["/Pages"]["/Count"])
