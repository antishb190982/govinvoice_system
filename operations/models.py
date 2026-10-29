from django.conf import settings
from django.db import models
from django.urls import reverse
from core.files import private_upload_path
import uuid


class BatchPreparation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=140)
    org_unit = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT)
    provider = models.ForeignKey("providers.Provider", on_delete=models.PROTECT)
    currency = models.CharField(max_length=3, default="MUR")
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    imports = models.ManyToManyField("imports.ImportBatch", blank=True, related_name="preparations")
    selected_invoice_ids = models.JSONField(default=list, blank=True)
    completed_batch = models.OneToOneField("InvoiceBatch", null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def get_absolute_url(self):
        return reverse("operations:prepare", args=[self.pk])


class InvoiceBatch(models.Model):
    reference = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=140)
    org_unit = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT, related_name="+")
    ministry = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT, related_name="+")
    provider = models.ForeignKey("providers.Provider", on_delete=models.PROTECT)
    currency = models.CharField(max_length=3)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def get_absolute_url(self):
        return reverse("operations:batch", args=[self.pk])

    def __str__(self):
        return self.reference

    @property
    def payment_instructions(self):
        from settlements.grouping import instructions_for
        return instructions_for(self)

    @property
    def lifecycle(self):
        if self.cancelled_at:
            return "Cancelled"
        live = self.payment_instructions.exclude(status="cancelled").first()
        if live:
            if live.status == "executed" and self.members.filter(active=True,
                    invoice__dispute_cases__status__in=["open", "authorisation", "refund_due"]).exists():
                return "Settled — dispute / refund open"
            return {"draft": "Settlement preparation", "pending": "Awaiting payment authorisation",
                    "pending_authorisation": "Awaiting payment authorisation", "authorised": "Authorised for payment",
                    "sent": "Sent to bank", "executed": "Settled"}.get(live.status, live.get_status_display())
        if self.members.filter(active=True, invoice__dispute_cases__status__in=["open", "authorisation", "refund_due"]).exists():
            return "On dispute hold"
        statuses = list(self.members.filter(active=True).values_list("invoice__status", flat=True))
        if not statuses:
            return "Closed — all members released"
        if statuses and all(status == "paid" for status in statuses):
            return "Closed — no payment due"
        if statuses and all(status == "approved" for status in statuses):
            return "Ready for settlement"
        if "in_approval" in statuses:
            return "In approval"
        return "Processing" if self.started_at else "Ready to process"


class BatchMember(models.Model):
    batch = models.ForeignKey(InvoiceBatch, on_delete=models.PROTECT, related_name="members")
    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.PROTECT, related_name="batch_memberships")
    active = models.BooleanField(default=True)
    original_amount = models.DecimalField(max_digits=16, decimal_places=3)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["batch", "invoice"], name="unique_batch_invoice"),
            models.UniqueConstraint(fields=["invoice"], condition=models.Q(active=True), name="one_active_invoice_batch"),
        ]


class DisputeCase(models.Model):
    is_surcharge = models.BooleanField(default=False)
    class Kind(models.TextChoices):
        ISSUE = "issue", "Invoice issue"
        WAIVER = "waiver", "Internal waiver"
        REFUND = "refund", "Refund request"

    class Status(models.TextChoices):
        OPEN = "open", "Awaiting finance review"
        AUTHORISATION = "authorisation", "Awaiting independent authorisation"
        RESOLVED = "resolved", "Resolved"
        REFUND_DUE = "refund_due", "Approved — refund outstanding"
        REFUNDED = "refunded", "Refund received"
        REJECTED = "rejected", "Rejected / no adjustment"
        WITHDRAWN = "withdrawn", "Withdrawn"

    reference = models.CharField(max_length=40, unique=True)
    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.PROTECT, related_name="dispute_cases")
    org_unit = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    item_description = models.CharField(max_length=250, blank=True, help_text="Blank means the whole invoice.")
    item_amount = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    amount = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    reason = models.TextField()
    evidence = models.FileField(upload_to=private_upload_path("invoice-disputes"), blank=True)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    opened_at = models.DateTimeField(auto_now_add=True)
    finance_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    finance_note = models.TextField(blank=True)
    review_fingerprint = models.CharField(max_length=64, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    applies_payable = models.BooleanField(default=False)
    invoice_snapshot = models.JSONField(default=dict)
    refund_reference = models.CharField(max_length=120, blank=True)
    refund_date = models.DateField(null=True, blank=True)
    refund_document = models.FileField(upload_to=private_upload_path("refund-receipts"), blank=True)
    refund_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-opened_at", "-pk"]

    def get_absolute_url(self):
        return reverse("operations:case", args=[self.pk])

    def __str__(self):
        return self.reference


class RefundReceipt(models.Model):
    case = models.ForeignKey(DisputeCase, on_delete=models.PROTECT, related_name="refund_receipts")
    amount = models.DecimalField(max_digits=16, decimal_places=3)
    reference = models.CharField(max_length=120, unique=True)
    received_date = models.DateField()
    document = models.FileField(upload_to=private_upload_path("refund-receipts"))
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def get_absolute_url(self):
        return reverse("operations:document", args=[self.case_id, f"receipt-{self.pk}"])
