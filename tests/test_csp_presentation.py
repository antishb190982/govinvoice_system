from pathlib import Path
import re
from types import SimpleNamespace

from django.template.loader import render_to_string


def test_ocr_review_labels_are_linked_to_selected_field_without_changing_editor():
    context = {"doc": SimpleNamespace(pk=1), "page": {
        "index": 0, "width": 600, "height": 800,
        "rects": [{"field": "invoice_number", "tone": "high", "x": 30, "y": 40,
                   "w": 100, "h": 20, "ty": 35, "tag": "Invoice number"}]}}
    review = render_to_string("ocr/_page.html", context)
    assert 'class="ocr-page ocr-review-page"' in review
    assert '<text class="zone-tag" data-field="invoice_number"' in review
    editor = render_to_string("ocr/_page.html", {**context, "editor": True})
    assert "ocr-review-page" not in editor and "ocr-canvas" in editor


def test_templates_do_not_require_csp_blocked_inline_style_attributes():
    root = Path(__file__).resolve().parents[1] / "templates"
    invalid = [str(path.relative_to(root)) for path in root.rglob("*.html")
               if re.search(r"\sstyle\s*=", path.read_text(), re.IGNORECASE)]
    assert not invalid, f"Use external CSS instead of inline style attributes: {invalid}"
