from django.conf import settings
from django.db import models
from django.urls import reverse

from core.files import private_upload_path


class Statement(models.Model):
    provider = models.ForeignKey("providers.Provider", on_delete=models.PROTECT)
    org_unit = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT)
    currency = models.CharField(max_length=3)
    statement_date = models.DateField()
    document = models.FileField(upload_to=private_upload_path("provider-statements"), blank=True)
    filename = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["-statement_date", "-pk"]

    def get_absolute_url(self):
        return reverse("reconciliation:statement", args=[self.pk])


class StatementRow(models.Model):
    class Status(models.TextChoices):
        MATCHED = "matched", "Reconciled"
        UNSORTED = "unsorted", "Unsorted — attention required"
        SUPERSEDED = "superseded", "Superseded by a newer statement"

    statement = models.ForeignKey(Statement, on_delete=models.CASCADE, related_name="rows")
    line_number = models.PositiveIntegerField()
    account_number = models.CharField(max_length=100, blank=True)
    invoice_number = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    raw = models.JSONField(default=dict)
    parse_error = models.TextField(blank=True)
    is_ledger_only = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNSORTED, db_index=True)
    reason = models.TextField(blank=True)
    invoice = models.ForeignKey("invoices.Invoice", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="reconciliation_rows")
    fingerprint = models.CharField(max_length=64, blank=True)
    assigned_unit = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    checked_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["statement_id", "line_number"]
        constraints = [models.UniqueConstraint(fields=["statement", "line_number"], name="unique_statement_line")]

    def get_absolute_url(self):
        return reverse("reconciliation:row", args=[self.pk])


class PaymentEvidence(models.Model):
    """Immutable invoice baseline and the evidence checked before bank release."""

    instruction = models.ForeignKey("settlements.SettlementInstruction", on_delete=models.CASCADE,
                                    related_name="reconciliation_evidence")
    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.PROTECT)
    row = models.ForeignKey(StatementRow, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["instruction", "invoice"], name="unique_payment_reconciliation")]


class BankStatement(models.Model):
    org_unit = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT)
    account_label = models.CharField(max_length=100, help_text="Stable bank-account alias; do not enter a full account number.")
    currency = models.CharField(max_length=3)
    statement_date = models.DateField()
    document = models.FileField(upload_to=private_upload_path("bank-statements"))
    filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def get_absolute_url(self):
        return reverse("reconciliation:bank_statement", args=[self.pk])


class BankRow(models.Model):
    class Status(models.TextChoices):
        MATCHED = "matched", "Bank reconciled"
        EXCEPTION = "exception", "Needs investigation"
        DUPLICATE = "duplicate", "Previously reconciled transaction"

    statement = models.ForeignKey(BankStatement, on_delete=models.CASCADE, related_name="rows")
    line_number = models.PositiveIntegerField()
    transaction_id = models.CharField(max_length=120, blank=True)
    settlement_reference = models.CharField(max_length=80, blank=True)
    provider_code = models.CharField(max_length=30, blank=True)
    bank_reference = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    value_date = models.DateField(null=True, blank=True)
    raw = models.JSONField(default=dict)
    parse_error = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.EXCEPTION, db_index=True)
    reason = models.TextField(blank=True)
    instruction = models.ForeignKey("settlements.SettlementInstruction", null=True, blank=True, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64, blank=True)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    checked_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    review_reason = models.TextField(blank=True)
    reviewed_fingerprint = models.CharField(max_length=64, blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="bank_exception_cases")
    resolution_due_date = models.DateField(null=True, blank=True, db_index=True)
    case_note = models.TextField(blank=True)
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["statement_id", "line_number"]
        constraints = [models.UniqueConstraint(fields=["statement", "line_number"], name="unique_bank_statement_line")]

    def get_absolute_url(self):
        return reverse("reconciliation:bank_row", args=[self.pk])


class BankTransactionKey(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    row = models.OneToOneField(BankRow, on_delete=models.PROTECT, related_name="transaction_key")


class BankAllocation(models.Model):
    # A bank debit covers a beneficiary transfer, or all transfers in a batch.
    line = models.OneToOneField("settlements.SettlementLine", on_delete=models.PROTECT, related_name="bank_allocation")
    row = models.ForeignKey(BankRow, on_delete=models.PROTECT, related_name="allocations")
