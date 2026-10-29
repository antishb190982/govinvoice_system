"""Scoped, audited workflow configuration without changing running task snapshots."""

import hashlib
import json
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.forms import BaseFormSet, formset_factory
from django.shortcuts import get_object_or_404, redirect, render

from core.models import AuditEvent
from org.access import active_memberships, has_role, scope, units_for, user_has_any_role
from org.models import Role
from workflow.models import ApprovalPolicy, ApprovalStep

DECISION_ROLES = [Role.VERIFIER, Role.APPROVER, Role.FINANCE, Role.AUTHORISER, Role.UNIT_ADMIN]
ROLE_CHOICES = [(role, role.label) for role in DECISION_ROLES]
STEP_FIELDS = ["order", "name", "role", "sla_hours", "requires_signature", "min_amount", "escalation_role"]


class PolicyForm(forms.ModelForm):
    version = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = ApprovalPolicy
        fields = ["name", "org_unit", "utility_type", "min_amount", "max_amount", "priority", "is_active"]

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user, [Role.UNIT_ADMIN])
        self.fields["org_unit"].label_from_instance = lambda unit: unit.indented_name
        self.fields["min_amount"].min_value = 0
        self.fields["max_amount"].min_value = 0

    def clean(self):
        data = super().clean()
        low, high = data.get("min_amount"), data.get("max_amount")
        if low is not None and low < 0:
            self.add_error("min_amount", "Use a non-negative amount.")
        if high is not None and (high < 0 or (low is not None and high < low)):
            self.add_error("max_amount", "The maximum must be at least the minimum.")
        return data


class StepForm(forms.Form):
    order = forms.IntegerField(min_value=1, max_value=65535, help_text="Lower numbers run first.")
    name = forms.CharField(max_length=120)
    role = forms.ChoiceField(choices=ROLE_CHOICES, initial=Role.APPROVER)
    sla_hours = forms.IntegerField(min_value=1, max_value=32767, initial=16, label="SLA working hours")
    requires_signature = forms.BooleanField(required=False, label="Require signing PIN and signature")
    min_amount = forms.DecimalField(min_value=0, max_digits=16, decimal_places=3, initial=0,
                                     label="Include from amount")
    escalation_role = forms.ChoiceField(choices=ROLE_CHOICES, initial=Role.UNIT_ADMIN)


class StepFormSet(BaseFormSet):
    def clean(self):
        if any(self.errors):
            return
        rows = [f.cleaned_data for f in self.forms if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        orders = [r["order"] for r in rows]
        if len(set(orders)) != len(orders):
            raise forms.ValidationError("Each step needs a different order number.")


Steps = formset_factory(StepForm, formset=StepFormSet, extra=1, can_delete=True,
                        min_num=1, validate_min=True, max_num=20, validate_max=True)


def policy_snapshot(policy):
    return {
        "name": policy.name, "org_unit": policy.org_unit_id, "utility_type": policy.utility_type,
        "min_amount": format(Decimal(policy.min_amount), ".3f"),
        "max_amount": format(Decimal(policy.max_amount), ".3f") if policy.max_amount is not None else None,
        "priority": policy.priority, "is_active": policy.is_active,
        "steps": [{field: str(getattr(step, field)) for field in STEP_FIELDS}
                  for step in policy.steps.order_by("order")],
    }


def policy_version(policy):
    return hashlib.sha256(json.dumps(policy_snapshot(policy), sort_keys=True).encode()).hexdigest()


def policy_nodes(policy):
    return [{"label": s.name, "role": s.get_role_display(), "sla": s.sla_hours,
             "signature": s.requires_signature, "threshold": s.min_amount, "state": "waiting",
             "current": False, "url": "", "reasons": []} for s in policy.steps.order_by("order")]


def readable_policies(user):
    queryset = ApprovalPolicy.objects.select_related("org_unit").prefetch_related("steps")
    if user.is_superuser:
        return queryset
    ancestors = set()
    for membership in active_memberships(user):
        ancestors.update(membership.org_unit.ancestor_ids)
    return queryset.filter(Q(pk__in=scope(ApprovalPolicy.objects.all(), user).values("pk")) |
                           Q(org_unit_id__in=ancestors))


@login_required
def policy_detail(request, pk):
    policy = get_object_or_404(readable_policies(request.user), pk=pk)
    return render(request, "workflow/policy_detail.html", {
        "policy": policy, "nodes": policy_nodes(policy),
        "can_edit": has_role(request.user, policy.org_unit, [Role.UNIT_ADMIN]),
    })


@login_required
def policy_form(request, pk=None):
    if not user_has_any_role(request.user, [Role.UNIT_ADMIN]):
        raise PermissionDenied
    manageable = scope(ApprovalPolicy.objects.all(), request.user, [Role.UNIT_ADMIN])
    policy = get_object_or_404(manageable, pk=pk) if pk else None
    initial_steps = list(policy.steps.values(*STEP_FIELDS)) if policy else [
        {"order": 1, "name": "Verification", "role": Role.VERIFIER, "sla_hours": 8,
         "min_amount": 0, "escalation_role": Role.UNIT_ADMIN, "requires_signature": False},
        {"order": 2, "name": "Approval", "role": Role.APPROVER, "sla_hours": 16,
         "min_amount": 0, "escalation_role": Role.UNIT_ADMIN, "requires_signature": True},
    ]
    initial = {"version": policy_version(policy)} if policy else {}
    if not policy:
        initial["org_unit"] = request.GET.get("unit")
    form = PolicyForm(request.POST or None, instance=policy, initial=initial, user=request.user)
    steps = Steps(request.POST or None, prefix="steps", initial=initial_steps)
    if request.method == "POST":
        form_valid, steps_valid = form.is_valid(), steps.is_valid()
        if form_valid and steps_valid:
            rows = [f.cleaned_data for f in steps if f.cleaned_data and not f.cleaned_data.get("DELETE")]
            minimum = form.cleaned_data["min_amount"]
            if form.cleaned_data["is_active"] and not any(
                    row["requires_signature"] and row["min_amount"] <= minimum for row in rows):
                form.add_error(None, "An active workflow needs a signature step covering its minimum amount.")
            else:
                with transaction.atomic():
                    locked = get_object_or_404(manageable.select_for_update(), pk=pk) if pk else None
                    if locked and form.cleaned_data["version"] != policy_version(locked):
                        form.add_error(None, "This workflow changed in another session. Reload before saving.")
                    else:
                        before = policy_snapshot(locked) if locked else None
                        saved = form.save()
                        saved.steps.all().delete()
                        for order, row in enumerate(sorted(rows, key=lambda r: r["order"]), 1):
                            values = {field: row[field] for field in STEP_FIELDS if field != "order"}
                            ApprovalStep.objects.create(policy=saved, order=order, **values)
                        AuditEvent.record("workflow.policy_saved", obj=saved, actor=request.user,
                                          org_unit_id=saved.org_unit_id,
                                          data={"before": before, "after": policy_snapshot(saved)})
                        messages.success(request, "Workflow saved. In-progress invoices retain their original steps.")
                        return redirect("workflow:policy", pk=saved.pk)
    return render(request, "workflow/policy_form.html", {
        "form": form, "steps": steps, "policy": policy,
        "nodes": policy_nodes(policy) if policy else [],
    })
