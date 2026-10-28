from django.conf import settings
from django.db import models
from django.urls import reverse

from core.fields import EncryptedTextField
from core.files import private_upload_path
from invoices.models import MONEY, Invoice
from org.models import OrgUnit
from providers.models import Provider


class SettlementInstruction(models.Model):
    """
    A payment instruction to the paying bank/treasury: the total to settle for
    each utility provider, built from approved invoices before their due dates.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_AUTHORISATION = "pending", "Awaiting authorisation"
        AUTHORISED = "authorised", "Authorised"
        SENT = "sent", "Sent to bank / treasury"
        EXECUTED = "executed", "Executed (paid)"
        CANCELLED = "cancelled", "Cancelled"

    reference = models.CharField(max_length=32, unique=True, editable=False)
    org_unit = models.ForeignKey(OrgUnit, on_delete=models.PROTECT, related_name="settlements")
    currency = models.CharField(max_length=3)
    cutoff_date = models.DateField(help_text="Includes approved invoices due on or before this date")
    pay_by = models.DateField(help_text="Earliest due date among included invoices")
    requested_execution_date = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    total_amount = models.DecimalField(**MONEY, default=0)
    notes = models.TextField(blank=True)

    payer_name = models.CharField(max_length=140)
    payer_iban = EncryptedTextField(blank=True)
    payer_bic = models.CharField(max_length=11, blank=True)

    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    prepared_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    authorise_by = models.DateTimeField(null=True, blank=True)
    authorised_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+")
    authorised_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateField(null=True, blank=True)
    bank_reference = models.CharField(max_length=80, blank=True)
    rejection_comment = models.TextField(blank=True)

    pdf = models.FileField(upload_to=private_upload_path("settlements"), blank=True)
    pdf_sha256 = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-prepared_at"]

    def __str__(self):
        return self.reference

    def get_absolute_url(self):
        return reverse("settlements:detail", args=[self.pk])


class SettlementLine(models.Model):
    """One beneficiary (provider) in an instruction, with its total."""

    instruction = models.ForeignKey(SettlementInstruction, on_delete=models.CASCADE, related_name="lines")
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(**MONEY)
    invoice_count = models.PositiveIntegerField()
    earliest_due = models.DateField()
    beneficiary_name = models.CharField(max_length=140)
    beneficiary_iban = EncryptedTextField(blank=True)
    beneficiary_bic = models.CharField(max_length=11, blank=True)
    end_to_end_id = models.CharField(max_length=35)

    class Meta:
        ordering = ["earliest_due", "provider__name"]


class SettlementItem(models.Model):
    line = models.ForeignKey(SettlementLine, on_delete=models.CASCADE, related_name="items")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="settlement_items")
    amount = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["invoice__due_date"]


class SettlementEvent(models.Model):
    """Signatures applied to the instruction PDF (preparer, authoriser)."""

    instruction = models.ForeignKey(SettlementInstruction, on_delete=models.CASCADE, related_name="signings")
    role = models.CharField(max_length=20)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    identity = models.ForeignKey("signatures.SigningIdentity", on_delete=models.PROTECT, related_name="+")
    signed_at = models.DateTimeField(auto_now_add=True)
    pdf_sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ["signed_at"]
