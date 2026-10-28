from django.conf import settings
from django.db import models

from core.files import private_upload_path
from providers.models import Provider

FIELD_CHOICES = [
    ("provider", "Utility provider"),
    ("account_number", "Account number"),
    ("invoice_number", "Invoice number"),
    ("issue_date", "Issue date"),
    ("due_date", "Due date"),
    ("period_start", "Period start"),
    ("period_end", "Period end"),
    ("amount_net", "Net amount"),
    ("amount_tax", "Tax / VAT"),
    ("amount_total", "Total payable"),
    ("consumption", "Consumption"),
    ("previous_reading", "Previous reading"),
    ("current_reading", "Current reading"),
    ("currency", "Currency"),
]
FIELD_LABELS = dict(FIELD_CHOICES)


class OcrDocument(models.Model):
    """
    One analysed invoice document (PDF or image): every page's rendered preview,
    the words found with their positions, and the field extraction with
    confidence and location for each value.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        DONE = "done", "Analysed"
        FAILED = "failed", "Failed"

    file = models.FileField(upload_to=private_upload_path("ocr"))
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=8)  # pdf, png, jpg, tif
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.QUEUED)
    error = models.TextField(blank=True)
    # [{"index": 0, "width": px, "height": px, "method": "text"|"ocr", "rotation": deg,
    #   "skew": deg, "mean_conf": 0-100, "words": [{"t": text, "c": conf, "b": [x0, y0, x1, y1]}]}]
    # Boxes are normalised to 0..1 of the page so they overlay any preview size.
    pages = models.JSONField(default=list)
    # {"field": {"value", "raw", "confidence", "page", "bbox", "source", "evidence"}}
    extraction = models.JSONField(default=dict)
    provider = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    provider_confidence = models.FloatField(default=0)
    template = models.ForeignKey("OcrTemplate", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    engine = models.CharField(max_length=80, blank=True)
    processing_ms = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.original_name

    @property
    def page_count(self):
        return len(self.pages)

    @property
    def used_ocr(self):
        return any(p.get("method") == "ocr" for p in self.pages)

    def preview_name(self, index):
        return f"ocr/previews/{self.pk}-{self.sha256[:16]}-p{index}.png"


class OcrTemplate(models.Model):
    """
    Where a provider prints each field. Zones are normalised page rectangles,
    optionally anchored to a nearby label so that shifted or rescaled scans
    still line up: {"field": {"page": 0, "bbox": [x0, y0, x1, y1],
    "anchor": "Total Amount Due", "anchor_bbox": [...]}}.
    """

    provider = models.OneToOneField(Provider, on_delete=models.CASCADE, related_name="ocr_template")
    zones = models.JSONField(default=dict)
    sample = models.ForeignKey(OcrDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    learned_from = models.PositiveIntegerField(default=0, help_text="Confirmed documents this template learned from")
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"OCR template · {self.provider}"
