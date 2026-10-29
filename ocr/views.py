import json

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from imports import parsers
from imports.models import ImportBatch, ImportRow
from imports.services import confirm_row
from ocr import services
from ocr.engine import DocumentError
from ocr.models import FIELD_CHOICES, FIELD_LABELS, OcrDocument, OcrTemplate
from org.access import can_view
from providers.models import Provider
from providers.views import is_master_data_admin

EDITABLE_FIELDS = [f for f, _ in FIELD_CHOICES if f in parsers.FIELDS]


def _can_see(user, doc):
    if user.is_superuser or doc.uploaded_by_id == user.pk:
        return True
    if any(can_view(user, inv.org_unit) for inv in doc.invoices.select_related("org_unit")):
        return True
    return is_master_data_admin(user) and OcrTemplate.objects.filter(sample=doc).exists()


def _get_doc(request, pk):
    doc = get_object_or_404(OcrDocument, pk=pk)
    if not _can_see(request.user, doc):
        raise Http404
    return doc


def _tone(conf):
    from django.conf import settings

    if conf is None:
        return "low"
    if conf >= settings.OCR_REVIEW_THRESHOLD:
        return "high"
    return "mid" if conf >= 0.5 else "low"


def _pages_with_boxes(doc, boxes):
    """Pages for the SVG overlay, with boxes converted to preview pixel coordinates."""
    out = []
    for page in doc.pages:
        w, h = page["width"], page["height"]
        rects = []
        for item in boxes:
            if item.get("page", 0) != page["index"] or not item.get("bbox"):
                continue
            b = item["bbox"]
            rects.append({**item, "x": round(b[0] * w, 1), "y": round(b[1] * h, 1),
                          "w": round((b[2] - b[0]) * w, 1), "h": round((b[3] - b[1]) * h, 1),
                          "ty": round(max(18, b[1] * h - 8), 1)})
        out.append({"index": page["index"], "width": w, "height": h, "rects": rects, "method": page.get("method"),
                    "mean_conf": page.get("mean_conf"), "rotation": page.get("rotation"), "skew": page.get("skew")})
    return out


class ConfirmForm(forms.Form):
    learn = forms.BooleanField(required=False, initial=False,
                               label="Suggest a template adjustment for future invoices")

    def __init__(self, *args, values=None, **kwargs):
        super().__init__(*args, **kwargs)
        for field in EDITABLE_FIELDS:
            self.fields[field] = forms.CharField(required=False, label=FIELD_LABELS[field],
                                                 initial=(values or {}).get(field) or "")
        self.order_fields(EDITABLE_FIELDS + ["learn"])


@login_required
def document(request, pk):
    doc = _get_doc(request, pk)
    row = ImportRow.objects.filter(ocr_document=doc).select_related("batch").first()
    can_confirm = bool(row and row.batch.status == ImportBatch.Status.STAGED and row.batch.uploaded_by_id == request.user.pk
                       and row.status in (ImportRow.Status.VALID, ImportRow.Status.INVALID))
    form = ConfirmForm(request.POST or None, values=(row.raw if row else {}))
    if request.method == "POST" and can_confirm and form.is_valid():
        values = {f: form.cleaned_data[f] for f in EDITABLE_FIELDS}
        row, learned = confirm_row(row, request.user, values, learn=form.cleaned_data["learn"])
        if row.status == ImportRow.Status.VALID:
            note = f" Learned positions for: {', '.join(FIELD_LABELS[f] for f in learned)}." if learned else ""
            messages.success(request, f"Row {row.row_number} confirmed.{note}")
            return redirect("imports:batch", pk=row.batch_id)
        messages.error(request, "Still not valid: " + "; ".join(row.errors))
    fields = []
    for field, label in FIELD_CHOICES:
        item = doc.extraction.get(field)
        if not item:
            fields.append({"field": field, "label": label, "value": None, "tone": "low"})
            continue
        fields.append({"field": field, "label": label, "value": item.get("display") or item.get("value"),
                       "confidence": item.get("confidence"), "pct": int((item.get("confidence") or 0) * 100),
                       "tone": _tone(item.get("confidence")), "source": item.get("source"),
                       "evidence": item.get("evidence"), "raw": item.get("raw"), "page": item.get("page", 0),
                       "bbox": item.get("bbox"), "label_bbox": item.get("label_bbox")})
    boxes = [{"field": f["field"], "tag": f["label"], "tone": f["tone"], "bbox": f.get("bbox"), "page": f.get("page", 0)}
             for f in fields if f.get("bbox")]
    boxes += [{"field": f["field"], "tag": "", "tone": "label", "bbox": f["label_bbox"], "page": f.get("page", 0)}
              for f in fields if f.get("label_bbox")]
    return render(request, "ocr/document.html", {
        "doc": doc, "pages": _pages_with_boxes(doc, boxes), "fields": fields, "row": row,
        "warnings": doc.extraction.get("_warnings", []), "form": form, "can_confirm": can_confirm,
        "can_edit_template": is_master_data_admin(request.user) and doc.provider_id,
    })


@login_required
@require_POST
def reanalyse(request, pk):
    doc = _get_doc(request, pk)
    row = ImportRow.objects.filter(ocr_document=doc).select_related("batch").first()
    try:
        if row and row.batch.status == ImportBatch.Status.STAGED:
            from imports.services import apply_ocr

            apply_ocr(row)
        else:
            services.analyse(doc, hint=doc.provider)
        messages.success(request, "Document analysed again with the current template.")
    except DocumentError as exc:
        messages.error(request, str(exc))
    return redirect("ocr:document", pk=doc.pk)


@login_required
def preview(request, pk, page):
    doc = _get_doc(request, pk)
    name = doc.preview_name(page)
    if page >= doc.page_count or not default_storage.exists(name):
        raise Http404
    response = FileResponse(default_storage.open(name, "rb"), content_type="image/png")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=600"
    return response


@login_required
def template_list(request):
    providers = Provider.objects.filter(is_active=True).select_related("ocr_template")
    rows = []
    for p in providers:
        t = getattr(p, "ocr_template", None)
        rows.append({"provider": p, "template": t, "fields": sorted((t.zones or {}).keys()) if t else []})
    recent = OcrDocument.objects.filter(status=OcrDocument.Status.DONE).select_related("provider")
    if not request.user.is_superuser:
        recent = recent.filter(uploaded_by=request.user)
    return render(request, "ocr/templates.html", {"rows": rows, "recent": recent[:12],
                                                   "can_edit": is_master_data_admin(request.user),
                                                   "field_labels": FIELD_LABELS})


@login_required
def template_edit(request, provider_pk):
    if not is_master_data_admin(request.user):
        raise PermissionDenied
    provider = get_object_or_404(Provider, pk=provider_pk)
    template, _ = OcrTemplate.objects.get_or_create(provider=provider)
    docs = OcrDocument.objects.filter(provider=provider, status=OcrDocument.Status.DONE)
    sample = None
    if request.GET.get("doc"):
        sample = docs.filter(pk=request.GET["doc"]).first()
    sample = sample or template.sample or docs.first()
    if request.method == "POST":
        try:
            zones = json.loads(request.POST.get("zones") or "{}")
        except ValueError:
            zones = None
        if not isinstance(zones, dict):
            messages.error(request, "The zones could not be read.")
        else:
            if sample:
                for field, zone in zones.items():
                    if isinstance(zone, dict) and not zone.get("anchor"):
                        anchor, box = services.anchor_near(sample, field, int(zone.get("page", 0)), zone.get("bbox", [0, 0, 0, 0]))
                        zone["anchor"], zone["anchor_bbox"] = anchor or "", box
            template.is_active = bool(request.POST.get("is_active"))
            services.save_zones(template, zones, request.user, sample=sample)
            messages.success(request, f"Template saved with {len(template.zones)} zones.")
            return redirect("ocr:template_edit", provider_pk=provider.pk)
    pages = _pages_with_boxes(sample, []) if sample else []
    return render(request, "ocr/template_edit.html", {
        "provider": provider, "template": template, "sample": sample, "pages": pages[:1] + pages[1:],
        "docs": docs[:20], "fields": [(f, FIELD_LABELS[f]) for f in EDITABLE_FIELDS],
        "zones_json": template.zones or {},
    })


