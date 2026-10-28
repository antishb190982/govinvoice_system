"""Synthetic 'scanned' utility invoices for OCR tests and demos."""

import io
import random
from datetime import date

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT, size)
    except OSError:
        return ImageFont.load_default()


def _arabic(text):
    import arabic_reshaper
    from bidi.algorithm import get_display

    return get_display(arabic_reshaper.reshape(text))


def invoice_image(provider_name="Power Co", account="ACC-10045", number="PWR-2609-0045",
                  issue=date(2026, 10, 3), due=date(2026, 11, 2), start=date(2026, 9, 1), end=date(2026, 9, 30),
                  net="125,000.00", vat="18,750.00", total="143,750.00", consumption="27,450", unit="kWh",
                  prev="410,200", cur="437,650", layout="columns", arabic_header=None, skew=0.0, noise=0.0,
                  blur=0.0, rotate90=False, scale=1.0, seed=1):
    """Draw an A4 invoice at ~200 dpi. layout: 'columns' (label left, value right),
    'stacked' (value under label) or 'boxed' (totals box at bottom right)."""
    random.seed(seed)
    w, h = int(1654 * scale), int(2339 * scale)
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    s = lambda v: int(v * scale)  # noqa: E731
    d.rectangle([s(90), s(90), w - s(90), s(250)], outline="black", width=s(3))
    d.text((s(120), s(115)), provider_name, font=_font(s(52), True), fill="black")
    if arabic_header:
        d.text((s(120), s(185)), _arabic(arabic_header), font=_font(s(34)), fill="black")
    d.text((w - s(560), s(130)), "TAX INVOICE", font=_font(s(44), True), fill="black")

    rows = [("Account No:", account), ("Invoice No:", number), ("Invoice Date:", f"{issue:%d/%m/%Y}"),
            ("Billing Period:", f"{start:%d/%m/%Y} to {end:%d/%m/%Y}"), ("Due Date:", f"{due:%d/%m/%Y}")]
    y = s(330)
    for label, value in rows:
        if layout == "stacked":
            d.text((s(130), y), label, font=_font(s(30)), fill="#333")
            d.text((s(130), y + s(42)), value, font=_font(s(36), True), fill="black")
            y += s(110)
        else:
            d.text((s(130), y), label, font=_font(s(34)), fill="#333")
            d.text((s(560), y), value, font=_font(s(34), True), fill="black")
            y += s(70)

    y += s(50)
    d.text((s(130), y), "Meter readings", font=_font(s(36), True), fill="black")
    y += s(70)
    for label, value in (("Previous Reading:", prev), ("Current Reading:", cur), ("Consumption:", f"{consumption} {unit}")):
        d.text((s(150), y), label, font=_font(s(32)), fill="#333")
        d.text((s(620), y), value, font=_font(s(32)), fill="black")
        y += s(60)

    y += s(80)
    totals = [("Amount before tax:", net), ("VAT (15%):", vat), ("Total Amount Due:", f"Rs {total}")]
    if layout == "boxed":
        bx = w - s(900)
        d.rectangle([bx - s(30), y - s(30), w - s(90), y + s(260)], outline="black", width=s(3))
        for label, value in totals:
            d.text((bx, y), label, font=_font(s(34), label.startswith("Total")), fill="black")
            d.text((bx + s(400), y), value, font=_font(s(34), label.startswith("Total")), fill="black")
            y += s(80)
    else:
        for label, value in totals:
            d.text((s(130), y), label, font=_font(s(36), label.startswith("Total")), fill="black")
            d.text((s(760), y), value, font=_font(s(36), label.startswith("Total")), fill="black")
            y += s(75)
    d.text((s(130), h - s(220)), "Please pay before the due date to avoid disconnection.", font=_font(s(28)), fill="#444")
    d.text((s(130), h - s(170)), "Payments: The Mauritius Commercial Bank, IBAN MU00 MCBL 0000 0000 0000 0000 000M UR", font=_font(s(28)), fill="#444")

    if noise:
        px = img.load()
        for _ in range(int(w * h * noise)):
            x, yy = random.randrange(w), random.randrange(h)
            px[x, yy] = (0, 0, 0) if random.random() < 0.5 else (255, 255, 255)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if skew:
        img = img.rotate(skew, resample=Image.BICUBIC, expand=True, fillcolor="white")
    if rotate90:
        img = img.rotate(90, expand=True, fillcolor="white")
    return img


def as_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt, quality=80) if fmt == "JPEG" else img.save(buf, fmt)
    return buf.getvalue()


def as_scanned_pdf(img):
    """Image-only PDF (no text layer), like a scanner produces."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PDF", resolution=200)
    return buf.getvalue()


# --- Look-alikes of real Mauritian bills (fictitious customer data) -----------------------

def _pdf_lines(lines, size=(595.275, 822.047)):
    """Text-layer PDF: [(x, y_from_top, text, font_size, bold)]."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    for x, y, text, fs, bold in lines:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", fs)
        c.drawString(x, size[1] - y, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def mt_statement_pdf(bill="FINV05123456-2609A", bill_acc="5500123", cust_acc="38000111", line="4650001",
                     issued="04/10/2026", due="03/11/2026", period=("01/09/2026", "30/09/2026"),
                     net="1,200.00", vat="180.00", total="1,380.00", arrears="0.00", name="MINISTRY OF TEST"):
    """Mauritius Telecom 'Statement of account' layout (Apache FOP text layer)."""
    month = "September 2026"
    total_due = total if arrears in ("0.00", "") else f"{float(total.replace(',', '')) + float(arrears.replace(',', '')):,.2f}"
    rows = [
        (45, 60, "Statement of account", 14, True),
        (45, 104, "VAT Reg. No.: VAT20070778", 8, False),
        (45, 112, "BRN.: C06006834", 8, False), (340, 112, f"Bill issued on:{issued}", 8, False),
        (45, 125, name, 8, True), (340, 125, f"Month:{month}", 8, False),
        (45, 138, "GOVERNMENT HOUSE", 8, False), (340, 138, f"Period:{period[0]} - {period[1]}", 8, False),
        (340, 151, f"Cust. Acc./Bill. Acc.:{cust_acc} / {bill_acc}", 8, False),
        (45, 151, "PORT LOUIS", 8, False), (340, 163, f"Line number:{line}", 8, False),
        (340, 176, f"Bill Number:{bill}", 8, False),
        (45, 190, "Mauritius Telecom Ltd, Edith Cavell Street, Port Louis", 7, False),
        (45, 203, f"VAT INVOICE Serial No: {bill}", 8, True), (340, 203, "Balance on previous bill: 0.00", 8, False),
        (340, 215, "Additional charges: 0.00", 8, False),
        (340, 227, "Payments received/credit adjustments: 0.00", 8, False),
        (340, 239, f"Amount still due as at: {issued} {arrears}", 8, False),
        (45, 263, "Charges for the period", 9, True), (442, 263, "VAT %", 8, False),
        (45, 295, "Access Charge 01/10/2026 31/10/2026 15 1,200.00", 8, False),
        (200, 514, f"TOTAL AMOUNT FOR THE PERIOD (before VAT): {net}", 8, True),
        (200, 532, f"Value Added Tax: {vat}", 8, True),
        (200, 550, f"TOTAL AMOUNT INCLUSIVE OF VAT: {total}", 8, True),
        (45, 572, f"Last date for payment is {due}", 8, True),
        (45, 700, "PAYMENT VOUCHER", 10, True),
        (45, 719, f"Month: {month}", 8, False), (340, 719, f"Bill issued on: {issued}", 8, False),
        (45, 732, f"Name of customer: {name}", 8, False), (340, 732, f"Bill Number: {bill}", 8, False),
        (45, 746, f"Cust. Acc./Bill. Acc.: {cust_acc} / {bill_acc}", 8, False), (340, 746, f"Arrears: {arrears}", 8, False),
        (45, 759, f"Line number: {line}", 8, False), (340, 759, f"Current bill: {total}", 8, False),
        (340, 773, f"Total amount due: {total_due}", 9, True),
    ]
    return _pdf_lines(rows)


def ceb_ebill_pdf(invoice="250012345678", contract="000118900001", meter="11600001", issued="12.10.2026",
                  due="11.11.2026", period=("22.08.26", "22.09.26"), cur="22,865", prev="22,699", units="166",
                  current="1,146.00", arrears="0.00"):
    """Central Electricity Board e-Bill layout."""
    total = f"{float(current.replace(',', '')) + float(arrears.replace(',', '')):,.2f}"
    rows = [
        (40, 40, "Central Electricity Board", 12, True),
        (40, 52, "CORPORATE OFFICE, P.O BOX 134", 7, False),
        (40, 74, "VAT REG NO: VAT22000591 | BRN : F07000041", 7, False),
        (40, 92, f"Invoice No. {invoice} Date {issued} Name MINISTRY OF TEST", 8, False),
        (40, 104, "Business Partner 0001400001 MRU 741D12A AddressGOVERNMENT HOUSE PORT LOUIS", 8, False),
        (40, 116, f"Contract Acct. {contract} Tariff 215", 8, False),
        (40, 140, "METER READING DATA", 8, True),
        (40, 152, f"Meter No. Current Rdg. Previous Rdg. Units Consumption Period {period[0]} to {period[1]}", 7, False),
        (40, 162, "22.09.26 22.08.26 Last 3 Consumptions (kWh) 306 / 207 / 197", 7, False),
        (40, 172, f"#{meter} {cur} {prev} {units} Next Planned Rdg. Date 20.11.2026", 7, False),
        (40, 192, f"Total units consumed(kWh) {units}", 8, False),
        (40, 210, "STATEMENT OF ACCOUNT Amount (Rs) MESSAGES", 8, True),
        (40, 222, "Previous charges as at 14.09.2026 1,366.00", 8, False),
        (40, 234, "Less Payments 1,366.00", 8, False),
        (40, 246, f"Balance from previous charges as at {due}", 8, False),
        (40, 256, f"(A) {arrears}", 8, False),
        (40, 268, "New Charges", 8, True),
        (40, 280, "Consumption Charges 976.00", 8, False),
        (40, 292, "Meter rental 20.00", 8, False),
        (40, 304, f"Current amount payable by {due} (B) {current}", 8, True),
        (40, 318, f"Total Amount Due (A+B) {total}", 8, True),
        (40, 400, f"Contract Acct. {contract}Current Amt {current}", 8, False),
        (40, 412, f"Due Date {due} TOTAL AMOUNT DUE{total}", 8, True),
    ]
    return _pdf_lines(rows, size=(595, 842))


def mt_summary_csv(rows):
    """Mauritius Telecom 'summary' export: semicolon-separated with a trailing separator."""
    head = ("Account No ID;Billing Name;Telephone number;Bill number;Issue Date; Period;Due Date;"
            "Current Amount Bill (incl VAT);Current VAT Bill;Current Amount Due;Arrears;Grand Total;Payment mode"
            + " " * 40)
    lines = [head]
    for r in rows:
        lines.append(";".join(r) + ";")
    return ("\n".join(lines) + "\n").encode()
