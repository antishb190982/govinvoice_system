"""Dedicated mail intake settings and durable delivery evidence."""
from django.db import models

from core.files import private_upload_path


class EbillMailbox(models.Model):
    name = models.CharField(max_length=120)
    address = models.EmailField(unique=True)
    relay_client = models.ForeignKey("integrations.ApiClient", null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="ebill_mailboxes")
    enabled = models.BooleanField(default=False)
    auto_stage = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    def save(self, *args, **kwargs):
        self.address = self.address.strip().lower()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} · {self.address}"


class EbillSender(models.Model):
    mailbox = models.ForeignKey(EbillMailbox, on_delete=models.PROTECT, related_name="senders")
    address = models.EmailField()
    provider = models.ForeignKey("providers.Provider", on_delete=models.PROTECT, related_name="ebill_senders")
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["address"]
        constraints = [models.UniqueConstraint(fields=["mailbox", "address"], name="ebill_sender_mailbox_unique")]

    def save(self, *args, **kwargs):
        self.address = self.address.strip().lower()
        return super().save(*args, **kwargs)


class EbillDelivery(models.Model):
    class State(models.TextChoices):
        RECEIVED = "received", "Received"
        REJECTED = "rejected", "Rejected"
        QUARANTINED = "quarantined", "Quarantined"
        HELD = "held", "Held for manual staging"
        STAGED = "staged", "Staged for invoice review"
        FAILED = "failed", "Staging failed"

    mailbox = models.ForeignKey(EbillMailbox, on_delete=models.PROTECT, related_name="deliveries")
    relay_client = models.ForeignKey("integrations.ApiClient", on_delete=models.PROTECT, related_name="+")
    delivery_key = models.CharField(max_length=64)
    sender = models.EmailField(blank=True)
    authenticated_sender = models.EmailField(blank=True)
    subject = models.CharField(max_length=200, blank=True)
    message_id = models.CharField(max_length=300, blank=True)
    received_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    raw_message = models.FileField(upload_to=private_upload_path("ebill-mail"))
    sha256 = models.CharField(max_length=64)
    authentication_passed = models.BooleanField(default=False)
    attachments_clean = models.BooleanField(default=False)
    provider = models.ForeignKey("providers.Provider", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    state = models.CharField(max_length=16, choices=State.choices, default=State.RECEIVED)
    reason = models.CharField(max_length=500, blank=True)
    import_batches = models.ManyToManyField("imports.ImportBatch", blank=True, related_name="ebill_deliveries")

    class Meta:
        ordering = ["-recorded_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["mailbox", "delivery_key"], name="ebill_delivery_mailbox_unique")]
