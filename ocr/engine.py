"""
Document reading: turns a PDF or image into pages of positioned words.

Born-digital PDF pages use their text layer (exact text, confidence 100).
Scanned pages and photos are rendered, cleaned (orientation, deskew,
contrast, denoise) and read by Tesseract. Every word carries a box
normalised to 0..1 of its page so the UI can highlight it on any preview.
"""

import io
import re
import time

from django.conf import settings
from PIL import Image, ImageFilter, ImageOps

Image.MAX_IMAGE_PIXELS = 80_000_000  # refuse decompression bombs

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
MIN_TEXT_LAYER_CHARS = 25
MAX_PAGES = 20


class DocumentError(Exception):
    pass


def normalise_digits(text):
    return (text or "").translate(ARABIC_DIGITS)


def _tesseract():
    import pytesseract

    if settings.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD
    return pytesseract


def engine_version():
    try:
        return f"tesseract {_tesseract().get_tesseract_version()}"
    except Exception:
        return "tesseract unavailable"


# --- Image clean-up ------------------------------------------------------------

def _fix_orientation(img):
    """Use Tesseract's orientation detection to undo 90/180/270° rotations."""
    try:
        osd = _tesseract().image_to_osd(img, config="--psm 0 -c min_characters_to_try=10")
        match = re.search(r"Rotate: (\d+)", osd)
        angle = int(match.group(1)) if match else 0
        conf = float(re.search(r"Orientation confidence: ([\d.]+)", osd).group(1))
    except Exception:
        return img, 0
    if angle and conf >= 1.5:
        return img.rotate(-angle, expand=True, fillcolor="white"), angle
    return img, 0


def _estimate_skew(gray):
    """Small-angle deskew by maximising the variance of row ink profiles."""
    import numpy as np

    small = gray.copy()
    small.thumbnail((900, 900))
    arr = np.asarray(small, dtype=np.uint8)
    ink = (arr < 140).astype(np.float32)
    if ink.mean() < 0.002:
        return 0.0
    best_angle, best_score = 0.0, -1.0
    base = Image.fromarray((ink * 255).astype(np.uint8))
    for angle in [a / 2 for a in range(-10, 11)]:
        rotated = np.asarray(base.rotate(angle, resample=Image.BILINEAR, fillcolor=0), dtype=np.float32)
        score = float(np.var(rotated.sum(axis=1)))
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle


def prepare_image(img):
    """Return (clean greyscale image, rotation applied, skew corrected)."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    # Bring the page to roughly 300 dpi A4 width, where Tesseract works best.
    target = 2480
    if img.width < 1600:
        scale = target / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    elif img.width > 4000:
        img.thumbnail((4000, 4000 * img.height // img.width))
    img, rotation = _fix_orientation(img)
    gray = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
    gray = gray.filter(ImageFilter.MedianFilter(3))
    skew = _estimate_skew(gray)
    if abs(skew) >= 0.5:
        gray = gray.rotate(skew, resample=Image.BICUBIC, expand=True, fillcolor=255)
    return gray, rotation, skew


# --- Word extraction ---------------------------------------------------------------

def ocr_words(gray):
    pt = _tesseract()
    data = pt.image_to_data(gray, lang=settings.OCR_LANGUAGES, output_type=pt.Output.DICT,
                            config="--oem 1 --psm 3 -c preserve_interword_spaces=1")
    w, h = gray.size
    words = []
    for i, text in enumerate(data["text"]):
        text = normalise_digits(text.strip())
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        x, y, bw, bh = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words += split_glued({
            "t": text, "c": round(conf, 1),
            "b": [round(x / w, 5), round(y / h, 5), round((x + bw) / w, 5), round((y + bh) / h, 5)],
            "l": f"{data['block_num'][i]}.{data['par_num'][i]}.{data['line_num'][i]}",
        })
    return words


GLUE_PATTERNS = [
    # "Number:FINV04616866-2609A" -> "Number:" + "FINV04616866-2609A" (but keep 12:30 times)
    re.compile(r"^(?P<a>[^\W\d][\w.()/-]*?:)(?P<b>\S+)$"),
    # "DUE1,146.00" -> "DUE" + "1,146.00" (an amount glued to an upper-case word)
    re.compile(r"^(?P<a>[A-Z]{2,}\.?)(?P<b>-?\d[\d,]*\.\d{1,3})$"),
    # "000118902086Current" -> "000118902086" + "Current" (a number glued to a capitalised word)
    re.compile(r"^(?P<a>[#]?\d[\d,.]*\d)(?P<b>[A-Z][a-z]{2,}\S*)$"),
]


def split_glued(word):
    """Split a word whose label and value were printed without a space; boxes split by character share."""
    for pattern in GLUE_PATTERNS:
        m = pattern.match(word["t"])
        if not m or not m.group("a") or not m.group("b"):
            continue
        a, b = m.group("a"), m.group("b")
        x0, y0, x1, y1 = word["b"]
        cut = x0 + (x1 - x0) * len(a) / len(word["t"])
        first = {**word, "t": a, "b": [x0, y0, round(cut, 5), y1]}
        second = {**word, "t": b, "b": [round(cut, 5), y0, x1, y1]}
        return split_glued(first) + split_glued(second)
    return [word]


def text_layer_words(pdf_page):
    """Words from a born-digital PDF page, grouped into lines by baseline."""
    w, h = float(pdf_page.width), float(pdf_page.height)
    raw = pdf_page.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.5, y_tolerance=2)
    raw.sort(key=lambda r: (round(r["top"] / 3), r["x0"]))
    words, line_id, last_top = [], 0, None
    for r in raw:
        if last_top is None or abs(r["top"] - last_top) > 3:
            line_id += 1
            last_top = r["top"]
        words += split_glued({"t": normalise_digits(r["text"]), "c": 100.0,
                              "b": [round(r["x0"] / w, 5), round(r["top"] / h, 5), round(r["x1"] / w, 5),
                                    round(r["bottom"] / h, 5)], "l": f"t.{line_id}"})
    return words


def _png(img, max_width=1240):
    preview = img.convert("RGB")
    if preview.width > max_width:
        preview = preview.resize((max_width, int(preview.height * max_width / preview.width)), Image.LANCZOS)
    buf = io.BytesIO()
    preview.save(buf, "PNG", optimize=True)
    return buf.getvalue(), preview.size


def read_document(content, kind):
    """Return (pages, previews, elapsed_ms). pages are JSON-ready; previews are PNG bytes."""
    started = time.monotonic()
    pages, previews = [], []
    if kind == "pdf":
        import pdfplumber
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(content)
        try:
            with pdfplumber.open(io.BytesIO(content)) as plumber:
                if len(plumber.pages) > MAX_PAGES:
                    raise DocumentError(f"More than {MAX_PAGES} pages; split the file first.")
                for index, ppage in enumerate(plumber.pages):
                    words = text_layer_words(ppage)
                    chars = sum(len(w["t"]) for w in words)
                    if chars >= MIN_TEXT_LAYER_CHARS:
                        image = pdf[index].render(scale=150 / 72).to_pil()
                        meta = {"method": "text", "rotation": 0, "skew": 0.0}
                    else:
                        image = pdf[index].render(scale=settings.OCR_DPI / 72).to_pil()
                        image, rotation, skew = prepare_image(image)
                        words = ocr_words(image)
                        meta = {"method": "ocr", "rotation": rotation, "skew": skew}
                    png, size = _png(image)
                    previews.append(png)
                    pages.append(_page(index, size, words, meta))
        finally:
            pdf.close()
    else:
        try:
            img = Image.open(io.BytesIO(content))
        except Exception as exc:
            raise DocumentError(f"The image could not be opened ({exc.__class__.__name__}).")
        frames = getattr(img, "n_frames", 1)
        if frames > MAX_PAGES:
            raise DocumentError(f"More than {MAX_PAGES} pages; split the file first.")
        for index in range(frames):
            img.seek(index)
            image, rotation, skew = prepare_image(img.copy())
            words = ocr_words(image)
            png, size = _png(image)
            previews.append(png)
            pages.append(_page(index, size, words, {"method": "ocr", "rotation": rotation, "skew": skew}))
    return pages, previews, int((time.monotonic() - started) * 1000)


def _page(index, size, words, meta):
    confs = [w["c"] for w in words]
    return {"index": index, "width": size[0], "height": size[1], "words": words,
            "mean_conf": round(sum(confs) / len(confs), 1) if confs else 0.0, **meta}


# --- Layout helpers ------------------------------------------------------------------

def lines_of(page):
    """Group a page's words into reading lines (top-to-bottom, left-to-right)."""
    groups = {}
    for i, w in enumerate(page["words"]):
        groups.setdefault(w["l"], []).append((i, w))
    lines = []
    for items in groups.values():
        items.sort(key=lambda iw: iw[1]["b"][0])
        boxes = [w["b"] for _, w in items]
        lines.append({
            "text": " ".join(w["t"] for _, w in items),
            "words": [w for _, w in items],
            "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)],
            "conf": sum(w["c"] for _, w in items) / len(items),
        })
    # Tesseract can split one visual line into blocks; merge lines that share a baseline.
    lines.sort(key=lambda ln: (ln["bbox"][1], ln["bbox"][0]))
    merged = []
    for line in lines:
        prev = merged[-1] if merged else None
        if prev and abs(prev["bbox"][1] - line["bbox"][1]) < 0.006 and abs(prev["bbox"][3] - line["bbox"][3]) < 0.008:
            words = sorted(prev["words"] + line["words"], key=lambda w: w["b"][0])
            prev.update({"words": words, "text": " ".join(w["t"] for w in words),
                         "bbox": [min(prev["bbox"][0], line["bbox"][0]), min(prev["bbox"][1], line["bbox"][1]),
                                  max(prev["bbox"][2], line["bbox"][2]), max(prev["bbox"][3], line["bbox"][3])],
                         "conf": sum(w["c"] for w in words) / len(words)})
        else:
            merged.append(line)
    return merged


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def words_in(page, bbox, pad=0.006):
    x0, y0, x1, y1 = bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad
    out = []
    for w in page["words"]:
        cx, cy = (w["b"][0] + w["b"][2]) / 2, (w["b"][1] + w["b"][3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            out.append(w)
    out.sort(key=lambda w: (round(w["b"][1], 2), w["b"][0]))
    return out
