from django.contrib import admin

from core.models import AuditEvent
from workflow.models import ApprovalPolicy, ApprovalStep


class StepInline(admin.TabularInline):
    model = ApprovalStep
    extra = 0


@admin.register(ApprovalPolicy)
class ApprovalPolicyAdmin(admin.ModelAdmin):
    list_display = ["name", "org_unit", "utility_type", "min_amount", "max_amount", "priority", "is_active"]
    inlines = [StepInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        AuditEvent.record("policy.saved", obj=form.instance, actor=request.user,
                          data={"steps": list(form.instance.steps.values("order", "name", "role", "sla_hours",
                                                                          "requires_signature", "min_amount"))})
