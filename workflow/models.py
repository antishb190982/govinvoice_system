from django.conf import settings
from django.db import models
from django.utils import timezone

from invoices.models import Invoice
from org.models import OrgUnit, Role
from providers.models import UtilityType


class ApprovalPolicy(models.Model):
    """
    Which approval chain an invoice follows. The most specific active policy
    wins: deepest org unit, then a matching utility type, then priority.
    """

    name = models.CharField(max_length=120)
    org_unit = models.ForeignKey(OrgUnit, on_delete=models.PROTECT, related_name="approval_policies",
                                 help_text="Applies to this unit and every unit below it")
    utility_type = models.CharField(max_length=20, choices=UtilityType.choices, blank=True,
                                    help_text="Leave blank for all utilities")
    min_amount = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    max_amount = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    priority = models.PositiveSmallIntegerField(default=100, help_text="Lower wins among equally specific policies")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["org_unit__path", "priority"]
        verbose_name_plural = "approval policies"

    def __str__(self):
        return self.name


class ApprovalStep(models.Model):
    policy = models.ForeignKey(ApprovalPolicy, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=20, choices=Role.choices)
    sla_hours = models.PositiveSmallIntegerField(default=16, help_text="Working hours allowed for this step")
    requires_signature = models.BooleanField(default=False)
    min_amount = models.DecimalField(max_digits=16, decimal_places=3, default=0,
                                     help_text="Step only applies at or above this amount")
    escalation_role = models.CharField(max_length=20, choices=Role.choices, default=Role.UNIT_ADMIN,
                                       help_text="Alerted on breach; may then decide the step too")

    class Meta:
        ordering = ["policy", "order"]
        constraints = [models.UniqueConstraint(fields=["policy", "order"], name="unique_step_order")]

    def __str__(self):
        return f"{self.order}. {self.name}"


class ApprovalTask(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting for earlier step"
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        RETURNED = "returned", "Returned"
        DISPUTED = "disputed", "Disputed"
        CANCELLED = "cancelled", "Cancelled"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="approval_tasks")
    round = models.PositiveSmallIntegerField(default=1)
    order = models.PositiveSmallIntegerField()
    # Step settings are copied so later policy edits do not rewrite history.
    step = models.ForeignKey(ApprovalStep, null=True, on_delete=models.SET_NULL, related_name="+")
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=20, choices=Role.choices)
    escalation_role = models.CharField(max_length=20, choices=Role.choices)
    sla_hours = models.PositiveSmallIntegerField()
    requires_signature = models.BooleanField(default=False)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.WAITING, db_index=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expedited = models.BooleanField(default=False)
    reminded_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    breached = models.BooleanField(default=False)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="approval_decisions")
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ["invoice", "round", "order"]

    def __str__(self):
        return f"{self.invoice.reference} r{self.round} · {self.name}"

    @property
    def eligible_roles(self):
        return [self.role, self.escalation_role] if self.escalated_at else [self.role]

    @property
    def is_overdue(self):
        return self.status == self.Status.PENDING and self.due_at and timezone.now() > self.due_at

    @property
    def decided_late(self):
        return bool(self.decided_at and self.due_at and self.decided_at > self.due_at)

    def remaining_fraction(self):
        if not (self.activated_at and self.due_at):
            return None
        total = (self.due_at - self.activated_at).total_seconds()
        if total <= 0:
            return 0.0
        return max(0.0, (self.due_at - timezone.now()).total_seconds() / total)
