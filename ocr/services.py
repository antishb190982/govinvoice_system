import hashlib
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction

from core.models import AuditEvent
from ocr import extract
from ocr.engine import DocumentError, engine_version, lines_of, read_document, union
from ocr.models import OcrDocument, OcrTemplate
from providers.models import Provider

log = logging.getLogger(__name__)


def create_document(user, content, name, kind):
    doc = OcrDocument(original_name=name[:255], sha256=hashlib.sha256(content).hexdigest(), kind=kind,
                      uploaded_by=user)
    doc.file.save(name, ContentFile(content), save=False)
    doc.save()
    return doc


def _template_for(provider):
    return OcrTemplate.objects.filter(provider=provider, is_active=True).first()


def analyse(doc, hint=None):
    """Read the document (if not yet read) and run field analysis. Returns warnings."""
    if not doc.pages:
        doc.file.open("rb")
        content = doc.file.read()
        doc.file.close()
        try:
            pages, previews, elapsed = read_document(content, doc.kind)
        except DocumentError as exc:
            doc.status, doc.error = OcrDocument.Status.FAILED, str(exc)
            doc.save(update_fields=["status", "error"])
            raise
        for index, png in enumerate(previews):
            name = doc.preview_name(index)
            if default_storage.exists(name):
                default_storage.delete(name)
            default_storage.save(name, ContentFile(png))
        doc.pages, doc.processing_ms, doc.engine = pages, elapsed, engine_version()
    if not any(p["words"] for p in doc.pages):
        doc.status, doc.error = OcrDocument.Status.FAILED, "No text could be read from the document."
        doc.save()
        raise DocumentError(doc.error)
    providers = Provider.objects.filter(is_active=True)
    extraction, provider, p_conf, template, warnings = extract.analyse(doc.pages, providers, hint, _template_for)
    extraction["_warnings"] = warnings
    doc.extraction, doc.provider, doc.provider_confidence, doc.template = extraction, provider, p_conf, template
    doc.status, doc.error = OcrDocument.Status.DONE, ""
    doc.save()
    return warnings


def as_import_raw(doc):
    """Map an extraction to the import row's raw fields."""
    raw = {}
    for field, item in doc.extraction.items():
        if field.startswith("_") or not isinstance(item, dict):
            continue
        raw[field] = item.get("value")
    return raw


def confidences(doc):
    return {f: i["confidence"] for f, i in doc.extraction.items() if isinstance(i, dict) and "confidence" in i}


def needs_review(doc):
    conf = confidences(doc)
    return any(conf.get(f, 0) < settings.OCR_REVIEW_THRESHOLD for f in extract.REQUIRED)


# --- Learning where a provider prints its fields ------------------------------------------

def _matches(field, word_text, value):
    parsed, _ = extract.parse_typed(field, word_text)
    if parsed is None:
        return False
    kind = extract.TYPES[field]
    try:
        if kind in ("amount", "number"):
            return Decimal(str(parsed)) == Decimal(str(value))
        if kind == "date":
            return parsed == (value if isinstance(value, date) else date.fromisoformat(str(value)))
    except (InvalidOperation, ValueError):
        return False
    return extract.ocr_equivalent(str(parsed), str(value))


def _value_start(group, field):
    """A located value must begin with the value itself (or a currency sign before an
    amount), so the recorded box never swallows the printed label."""
    first = group[0]["t"].strip()
    if any(ch.isdigit() for ch in first):
        return True
    if extract.TYPES[field] == "amount" and first.rstrip(".:").upper() in extract.CURRENCIES | set(extract.CURRENCY_ALIASES):
        return len(group) > 1
    if extract.TYPES[field] == "date":
        import re

        return bool(re.match(extract.MONTHS, first.lower()))
    return False


def locate(doc, field, value):
    """Find where a confirmed value is printed: (page, bbox, anchor_text, anchor_bbox) or None."""
    if value in (None, "") or field not in extract.TYPES:
        return None
    for page in doc.pages:
        lines = lines_of(page)
        for li, line in enumerate(lines):
            words = line["words"]
            for i in range(len(words)):
                for span in (1, 2, 3, 4):
                    group = words[i:i + span]
                    if len(group) < span:
                        break
                    if not _value_start(group, field):
                        break
                    if _matches(field, " ".join(w["t"] for w in group), value):
                        box = union([w["b"] for w in group])
                        anchor, anchor_box = _anchor_for(field, lines, li, i)
                        return page["index"], box, anchor, anchor_box
    return None


def _anchor_for(field, lines, line_index, word_index):
    labels = extract.LABELS["period" if field in ("period_start", "period_end") else field]
    line = lines[line_index]
    found = [(end - start, label, start, end) for label, score, start, end in extract.find_labels(line, labels)
             if end <= word_index]
    if found:
        _, label, start, end = max(found)
        return label, union([w["b"] for w in line["words"][start:end]])
    if line_index > 0:
        above = lines[line_index - 1]
        for label, score, start, end in extract.find_labels(above, labels):
            return label, union([w["b"] for w in above["words"][start:end]])
    return None, None


def _zone(box):
    """Widen a value box so longer or shorter values in future invoices still fall inside it."""
    width = box[2] - box[0]
    return [round(max(0.0, box[0] - 0.01), 5), round(max(0.0, box[1] - 0.006), 5),
            round(min(1.0, box[2] + max(0.06, width * 0.6)), 5), round(min(1.0, box[3] + 0.006), 5)]


@transaction.atomic
def learn(doc, provider, confirmed, user):
    """Record where each confirmed value sits, building the provider's template."""
    template, _ = OcrTemplate.objects.select_for_update().get_or_create(provider=provider)
    learned = []
    zones = dict(template.zones or {})
    for field, value in confirmed.items():
        if field not in extract.TYPES or field == "currency":
            continue
        where = locate(doc, field, value)
        if not where:
            continue
        page, box, anchor, anchor_box = where
        zones[field] = {"page": page, "bbox": _zone(box), "anchor": anchor or "", "anchor_bbox": anchor_box}
        learned.append(field)
    if learned:
        template.zones = zones
        template.sample = doc
        template.learned_from += 1
        template.updated_by = user
        template.save()
        AuditEvent.record("ocr.template_learned", obj=template, actor=user,
                          data={"provider": provider.short_code, "fields": learned, "document": doc.pk})
    return learned


def save_zones(template, zones, user, sample=None):
    clean = {}
    for field, zone in (zones or {}).items():
        if field not in extract.TYPES:
            continue
        try:
            bbox = [min(1.0, max(0.0, float(v))) for v in zone["bbox"]][:4]
            page = int(zone.get("page", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        anchor = str(zone.get("anchor") or "")[:80]
        anchor_box = zone.get("anchor_bbox")
        if anchor_box:
            try:
                anchor_box = [float(v) for v in anchor_box][:4]
            except (TypeError, ValueError):
                anchor_box = None
        clean[field] = {"page": page, "bbox": bbox, "anchor": anchor if anchor_box else "", "anchor_bbox": anchor_box}
    template.zones = clean
    if sample:
        template.sample = sample
    template.updated_by = user
    template.save()
    AuditEvent.record("ocr.template_saved", obj=template, actor=user,
                      data={"provider": template.provider.short_code, "fields": sorted(clean)})
    return template


def process_queue(limit=10):
    """Analyse queued import rows (large uploads are processed off the request)."""
    from imports.models import ImportRow
    from imports.services import apply_ocr

    done = 0
    for row in ImportRow.objects.filter(status=ImportRow.Status.ANALYSING).select_related(
            "ocr_document", "batch__provider", "batch__uploaded_by")[:limit]:
        try:
            apply_ocr(row)
        except Exception:
            log.exception("OCR failed for import row %s", row.pk)
        done += 1
    return done


def anchor_near(doc, field, page_index, bbox):
    """Find a printed label for `field` just left of, or just above, a zone drawn by hand."""
    if field not in extract.TYPES or page_index >= len(doc.pages):
        return None, None
    labels = extract.LABELS["period" if field in ("period_start", "period_end") else field]
    best = None
    for line in lines_of(doc.pages[page_index]):
        for label, score, start, end in extract.find_labels(line, labels):
            box = union([w["b"] for w in line["words"][start:end]])
            same_row = abs(box[1] - bbox[1]) < 0.02 and box[2] <= bbox[0] + 0.02
            above = 0 <= bbox[1] - box[3] < 0.05 and box[0] < bbox[2] and box[2] > bbox[0] - 0.05
            if same_row or above:
                distance = abs(bbox[0] - box[2]) + abs(bbox[1] - box[1])
                rank = (round(distance, 3), -len(label))  # nearest, then the most specific label
                if best is None or rank < best[0]:
                    best = (rank, label, box)
    return (best[1], best[2]) if best else (None, None)
