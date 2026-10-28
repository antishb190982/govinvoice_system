from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse

from core.files import private_upload_path
from org.models import OrgUnit
from providers.models import Provider, ServiceAccount, UtilityType

MONEY = {"max_digits": 16, "decimal_places": 3}


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        IN_APPROVAL = "in_approval", "In approval"
        RETURNED = "returned", "Returned for correction"
        APPROVED = "approved", "Approved – awaiting settlement"
        SCHEDULED = "scheduled", "In settlement instruction"
        PAID = "paid", "Paid"
        DISPUTED = "disputed", "Disputed with provider"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual entry"
        SPREADSHEET = "spreadsheet", "Spreadsheet import"
        PDF = "pdf", "PDF extraction"
        OCR = "ocr", "Scanned document (OCR)"
        API = "api", "API"

    EDITABLE = {Status.DRAFT, Status.RETURNED}
    OPEN = {Status.DRAFT, Status.IN_APPROVAL, Status.RETURNED, Status.APPROVED, Status.SCHEDULED, Status.DISPUTED}
    UNPAID = {Status.IN_APPROVAL, Status.RETURNED, Status.APPROVED, Status.SCHEDULED, Status.DISPUTED}

    reference = models.CharField(max_length=32, unique=True, editable=False)
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="invoices")
    service_account = models.ForeignKey(ServiceAccount, on_delete=models.PROTECT, related_name="invoices")
    # Copied from the service account at intake so history survives transfers.
    org_unit = models.ForeignKey(OrgUnit, on_delete=models.PROTECT, related_name="invoices")
    utility_type = models.CharField(max_length=20, choices=UtilityType.choices, editable=False)

    invoice_number = models.CharField(max_length=60)
    issue_date = models.DateField()
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    due_date = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, blank=True, help_text="Defaults to the provider's currency")
    amount_net = models.DecimalField(**MONEY, default=Decimal("0"))
    amount_tax = models.DecimalField(**MONEY, default=Decimal("0"))
    amount_total = models.DecimalField(**MONEY)
    arrears = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True,
                                  help_text="Unpaid balance from earlier bills printed on this one (not part of the total)")
    consumption = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    consumption_unit = models.CharField(max_length=10, blank=True)
    previous_reading = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    current_reading = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    document = models.FileField(upload_to=private_upload_path("invoices"), blank=True)
    document_name = models.CharField(max_length=255, blank=True)
    document_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    flags = models.JSONField(default=list, blank=True)

    must_approve_by = models.DateTimeField(null=True, blank=True,
                                           help_text="Approval must complete by this time to settle before the due date")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateField(null=True, blank=True)
    payment_reference = models.CharField(max_length=80, blank=True)

    ocr_document = models.ForeignKey("ocr.OcrDocument", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="invoices")
    import_batch = models.ForeignKey("imports.ImportBatch", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="invoices")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_date", "id"]
        constraints = [
            # The same provider invoice can never be entered twice.
            models.UniqueConstraint(fields=["provider", "invoice_number"], name="unique_provider_invoice"),
            models.CheckConstraint(condition=models.Q(amount_total__gte=0), name="invoice_total_non_negative"),
        ]
        indexes = [models.Index(fields=["org_unit", "status"]), models.Index(fields=["issue_date"])]

    def __str__(self):
        return f"{self.reference} · {self.provider.short_code} {self.invoice_number}"

    def get_absolute_url(self):
        return reverse("invoices:detail", args=[self.pk])

    @property
    def is_editable(self):
        return self.status in self.EDITABLE

    @property
    def is_unpaid(self):
        return self.status in self.UNPAID

    def snapshot(self):
        """Fields an approval signature commits to."""
        return {
            "reference": self.reference,
            "provider": self.provider.short_code,
            "service_account": self.service_account.account_number,
            "org_unit": self.org_unit.code,
            "invoice_number": self.invoice_number,
            "issue_date": self.issue_date.isoformat(),
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "due_date": self.due_date.isoformat(),
            "currency": self.currency,
            "amount_net": str(self.amount_net),
            "amount_tax": str(self.amount_tax),
            "amount_total": str(self.amount_total),
            "consumption": str(self.consumption) if self.consumption is not None else None,
            "arrears": str(self.arrears) if self.arrears is not None else None,
            "document_sha256": self.document_sha256,
        }


class Flag:
    """Codes stored in Invoice.flags."""

    AMOUNT_SPIKE = "amount_spike"
    CONSUMPTION_SPIKE = "consumption_spike"
    TOTAL_MISMATCH = "total_mismatch"
    READING_MISMATCH = "reading_mismatch"
    PERIOD_OVERLAP = "period_overlap"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    SAME_DOCUMENT = "same_document"
    LATE_ARRIVAL = "late_arrival"
    OVERDUE = "overdue"
    NOTHING_TO_PAY = "nothing_to_pay"
    ARREARS = "arrears"

    LABELS = {
        AMOUNT_SPIKE: "Amount well above this account's recent average",
        CONSUMPTION_SPIKE: "Consumption well above this account's recent average",
        TOTAL_MISMATCH: "Net + tax does not equal the total",
        READING_MISMATCH: "Meter readings do not match the consumption",
        PERIOD_OVERLAP: "Billing period overlaps another invoice for this account",
        POSSIBLE_DUPLICATE: "Same account, period and amount as another invoice",
        SAME_DOCUMENT: "The same file was already attached to another invoice",
        LATE_ARRIVAL: "Received too close to the due date for the normal approval SLA",
        OVERDUE: "Past due date and not paid",
        NOTHING_TO_PAY: "Zero amount – closed without payment once approved",
        ARREARS: "The bill shows an unpaid balance from earlier bills",
    }
    SEVERE = {TOTAL_MISMATCH, POSSIBLE_DUPLICATE, SAME_DOCUMENT, READING_MISMATCH}


class InvoiceComment(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
