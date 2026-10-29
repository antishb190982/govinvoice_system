from django.conf import settings
from django.db import models
from django.utils import timezone

from core.files import private_upload_path
from providers.models import Provider


class ImportTemplate(models.Model):
    """
    How to read one provider's files. Spreadsheet templates map our fields to
    column headers; PDF templates give a regular expression per field whose
    first group captures the value. Without a template, common header names
    and invoice wording are recognised automatically.
    """

    class Kind(models.TextChoices):
        SPREADSHEET = "spreadsheet", "Spreadsheet (XLSX/XLS/CSV)"
        PDF = "pdf", "PDF"

    name = models.CharField(max_length=120)
    provider = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.CASCADE, related_name="templates")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    sheet_name = models.CharField(max_length=60, blank=True)
    header_row = models.PositiveSmallIntegerField(default=1)
    column_map = models.JSONField(default=dict, blank=True,
                                  help_text='e.g. {"account_number": "Contract No", "amount_total": "Net Payable"}')
    pdf_patterns = models.JSONField(default=dict, blank=True,
                                    help_text='e.g. {"invoice_number": "Bill No[.:]\\\\s*(\\\\S+)"}')
    date_format = models.CharField(max_length=20, blank=True, help_text="strftime format, e.g. %d/%m/%Y")
    decimal_comma = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class ImportBatch(models.Model):
    class Status(models.TextChoices):
        STAGED = "staged", "Staged for review"
        COMMITTED = "committed", "Committed"
        DISCARDED = "discarded", "Discarded"

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    provider = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    template = models.ForeignKey(ImportTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=12)
    original_name = models.CharField(max_length=255)
    file = models.FileField(upload_to=private_upload_path("imports"))
    sha256 = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.STAGED)
    created_at = models.DateTimeField(auto_now_add=True)
    received_date = models.DateField(default=timezone.localdate)
    intake_source = models.CharField(max_length=12, default="upload")
    intake_notes = models.JSONField(default=list, blank=True)
    registration_mode = models.BooleanField(default=False)
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Import #{self.pk} {self.original_name}"


class ImportRow(models.Model):
    class Status(models.TextChoices):
        ANALYSING = "analysing", "Analysing document"
        VALID = "valid", "Ready"
        INVALID = "invalid", "Has errors"
        COMMITTED = "committed", "Imported"
        SKIPPED = "skipped", "Skipped"

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    source_name = models.CharField(max_length=255, blank=True)
    source_file = models.FileField(upload_to=private_upload_path("imports"), blank=True)
    source_sha256 = models.CharField(max_length=64, blank=True)
    raw = models.JSONField(default=dict)
    data = models.JSONField(default=dict)
    errors = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=Status.choices)
    include = models.BooleanField(default=True)
    ocr_document = models.ForeignKey("ocr.OcrDocument", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="import_rows")
    confidence = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    # Low-confidence extractions must be checked and confirmed by a person before import.
    needs_review = models.BooleanField(default=False)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    invoice = models.ForeignKey("invoices.Invoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    @property
    def processing(self):
        from datetime import date
        from invoices.intake_dates import assess_dates

        return assess_dates(
            date.fromisoformat(self.data["issue_date"]) if self.data.get("issue_date") else None,
            date.fromisoformat(self.data["due_date"]) if self.data.get("due_date") else None,
            date.fromisoformat(self.data["received_date"]) if self.data.get("received_date") else self.batch.received_date,
            timezone.localdate(self.batch.created_at),
        )

    class Meta:
        ordering = ["row_number"]


class RegistrationRequest(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Awaiting unit administrator"
        READY = "ready", "Registration complete"

    batch = models.OneToOneField(ImportBatch, on_delete=models.PROTECT, related_name="registration_request")
    org_unit = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                      on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-created_at"]
