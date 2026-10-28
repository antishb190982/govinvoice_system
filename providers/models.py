from django.db import models

from core.fields import EncryptedTextField
from org.models import OrgUnit


class UtilityType(models.TextChoices):
    ELECTRICITY = "electricity", "Electricity"
    WATER = "water", "Water & sewerage"
    GAS = "gas", "Gas"
    TELECOM = "telecom", "Telephone / mobile"
    INTERNET = "internet", "Internet / data"
    WASTE = "waste", "Waste collection"
    FUEL = "fuel", "Fuel"
    DISTRICT_COOLING = "cooling", "District cooling"
    OTHER = "other", "Other"


CONSUMPTION_UNITS = {
    UtilityType.ELECTRICITY: "kWh",
    UtilityType.WATER: "m³",
    UtilityType.GAS: "m³",
    UtilityType.TELECOM: "min",
    UtilityType.INTERNET: "GB",
    UtilityType.WASTE: "t",
    UtilityType.FUEL: "L",
    UtilityType.DISTRICT_COOLING: "RTh",
    UtilityType.OTHER: "",
}


class Provider(models.Model):
    name = models.CharField(max_length=200, unique=True)
    short_code = models.CharField(max_length=20, unique=True)
    utility_type = models.CharField(max_length=20, choices=UtilityType.choices)
    tax_number = models.CharField(max_length=60, blank=True)
    invoice_number_pattern = models.CharField(
        max_length=200, blank=True,
        help_text=r"Regular expression for this provider's bill numbers, e.g. ^FINV\d{8}-\d{4}[A-Z]$. "
                  "Used to recognise the provider in files that do not name it.")
    identifiers = models.TextField(blank=True, help_text="Other names printed on its invoices (one per line, any "
                                                          "language), used to recognise scanned invoices")
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)
    # Payment details are encrypted at rest and every change is audited.
    bank_name = models.CharField(max_length=120, blank=True)
    bank_bic = models.CharField(max_length=11, blank=True)
    bank_iban = EncryptedTextField(blank=True)
    bank_account_name = models.CharField(max_length=140, blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    currency = models.CharField(max_length=3, default="MUR")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def masked_iban(self):
        iban = (self.bank_iban or "").replace(" ", "")
        return f"{iban[:4]} •••• {iban[-4:]}" if len(iban) > 8 else ("••••" if iban else "")

    @property
    def consumption_unit(self):
        return CONSUMPTION_UNITS.get(self.utility_type, "")


class ServiceAccount(models.Model):
    """A provider account or meter, owned by the org unit that consumes it."""

    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="accounts")
    org_unit = models.ForeignKey(OrgUnit, on_delete=models.PROTECT, related_name="service_accounts")
    account_number = models.CharField(max_length=60)
    meter_number = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=200, blank=True)
    premises = models.CharField(max_length=200, blank=True, help_text="Building / location served")
    tariff = models.CharField(max_length=60, blank=True)
    budget_line = models.CharField(max_length=60, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["provider__name", "account_number"]
        constraints = [models.UniqueConstraint(fields=["provider", "account_number"], name="unique_provider_account")]

    def __str__(self):
        label = f"{self.provider.short_code} {self.account_number}"
        return f"{label} – {self.premises}" if self.premises else label


class BankDetailsChange(models.Model):
    """
    Beneficiary bank details change only under dual control: one administrator
    requests, a different one approves. Protects against payment-diversion fraud.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="bank_changes")
    bank_name = models.CharField(max_length=120)
    bank_bic = models.CharField(max_length=11, blank=True)
    bank_iban = EncryptedTextField()
    bank_account_name = models.CharField(max_length=140)
    evidence_note = models.TextField(help_text="How the change was verified, e.g. call-back to a known contact")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
