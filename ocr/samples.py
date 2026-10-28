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
