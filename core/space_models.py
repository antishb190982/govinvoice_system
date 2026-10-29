from django.conf import settings
from django.db import models


class SpaceFolder(models.Model):
    class Destination(models.TextChoices):
        PREPARATION = "preparation", "Batch preparation"
        WORK = "work", "Batches in progress"
        PENDING = "pending", "Batches pending approval"
        REJECTED = "rejected", "Returned / rejected batches"
        COMPLETED = "completed", "Completed batches"
        ALL = "all", "All my batches"
        INVOICES = "invoices", "My invoices"
        SERVICES = "services", "Services in my units"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="space_folders")
    label = models.CharField(max_length=100)
    destination = models.CharField(max_length=20, choices=Destination.choices, default=Destination.WORK)
    org_unit = models.ForeignKey("org.OrgUnit", null=True, blank=True, on_delete=models.PROTECT)
    provider = models.ForeignKey("providers.Provider", null=True, blank=True, on_delete=models.PROTECT)
    notes = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label", "pk"]
        constraints = [models.UniqueConstraint(fields=["owner", "label"], name="space_folder_owner_label_unique")]

    def __str__(self):
        return self.label
