from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class ProcessingSchedule(models.Model):
    class Frequency(models.TextChoices):
        ONCE = "once", "Once"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="processing_schedules")
    name = models.CharField(max_length=140)
    org_unit = models.ForeignKey("org.OrgUnit", on_delete=models.PROTECT)
    provider = models.ForeignKey("providers.Provider", null=True, blank=True, on_delete=models.PROTECT)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY)
    next_due_date = models.DateField(default=timezone.localdate, db_index=True)
    anchor_day = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_due_date", "pk"]

    def get_absolute_url(self):
        return reverse("core:work_schedule", args=[self.pk])


class ProcessingCompletion(models.Model):
    schedule = models.ForeignKey(ProcessingSchedule, on_delete=models.PROTECT, related_name="completions")
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name_snapshot = models.CharField(max_length=140)
    due_date = models.DateField()
    completed_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True, max_length=2000)

    class Meta:
        ordering = ["-completed_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["schedule", "due_date"], name="one_processing_completion_per_due")]
