"""Personal recurring work plans; completion never processes a financial record."""
import calendar
from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import AuditEvent, ProcessingCompletion, ProcessingSchedule
from core.provider_scope import available_providers
from org.access import units_for
from providers.models import ServiceAccount


class ScheduleForm(forms.ModelForm):
    class Meta:
        model = ProcessingSchedule
        fields = ["name", "org_unit", "provider", "frequency", "next_due_date", "is_active", "notes"]
        labels = {"next_due_date": "Next processing date", "is_active": "Schedule active"}
        widgets = {"next_due_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"next_due_date": "A past date creates overdue personal work. Completing a cycle does not approve or pay bills."}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        self.fields["provider"].queryset = available_providers(user)
        self.fields["provider"].empty_label = "All providers in this unit"

    def clean(self):
        data = super().clean()
        unit, provider = data.get("org_unit"), data.get("provider")
        if unit and provider and not ServiceAccount.objects.filter(
                provider=provider, org_unit__path__startswith=unit.path).exists():
            self.add_error("provider", "This provider has no account allocated to the selected unit.")
        if self.instance.pk and data.get("is_active") and data.get("next_due_date") and (
                self.instance.completions.filter(due_date=data["next_due_date"]).exists()):
            self.add_error("next_due_date", "This cycle is already complete. Choose a new processing date before activating it.")
        return data


def owned_schedules(user):
    return ProcessingSchedule.objects.filter(owner=user, org_unit__in=units_for(user)).select_related(
        "org_unit", "provider", "owner")


def following_date(schedule):
    if schedule.frequency == "daily":
        return schedule.next_due_date + timedelta(days=1)
    if schedule.frequency == "weekly":
        return schedule.next_due_date + timedelta(days=7)
    month = schedule.next_due_date.month % 12 + 1
    year = schedule.next_due_date.year + (schedule.next_due_date.month == 12)
    return date(year, month, min(schedule.anchor_day, calendar.monthrange(year, month)[1]))


@transaction.atomic
def complete_cycle(user, pk, due, note=""):
    schedule = get_object_or_404(owned_schedules(user).select_for_update(), pk=pk)
    if ProcessingCompletion.objects.filter(schedule=schedule, due_date=due).exists():
        return schedule, False
    if not schedule.is_active or schedule.next_due_date != due:
        raise ValidationError("This schedule has changed. Reload before completing its current cycle.")
    if len(note) > 2000:
        raise ValidationError("Completion notes must not exceed 2,000 characters.")
    ProcessingCompletion.objects.create(schedule=schedule, completed_by=user,
        name_snapshot=schedule.name, due_date=due, note=note.strip())
    if schedule.frequency == "once":
        schedule.is_active = False
    else:
        schedule.next_due_date = following_date(schedule)
    schedule.save(update_fields=["is_active", "next_due_date", "updated_at"])
    AuditEvent.record("work.schedule_completed", obj=schedule, actor=user, org_unit_id=schedule.org_unit_id,
        data={"due_date": due.isoformat(), "next_due_date": schedule.next_due_date.isoformat(),
              "active": schedule.is_active, "note": note.strip()})
    return schedule, True


@login_required
def schedule_form(request, pk=None):
    schedule = get_object_or_404(owned_schedules(request.user), pk=pk) if pk else None
    old_due = schedule.next_due_date if schedule else None
    old_frequency = schedule.frequency if schedule else None
    form = ScheduleForm(request.POST if request.method == "POST" else None,
        instance=schedule, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            saved.owner = request.user
            if old_due != saved.next_due_date or old_frequency != saved.frequency:
                saved.anchor_day = saved.next_due_date.day
            saved.save()
            AuditEvent.record("work.schedule_updated" if pk else "work.schedule_created", obj=saved,
                actor=request.user, org_unit_id=saved.org_unit_id,
                data={"frequency": saved.frequency, "next_due_date": saved.next_due_date.isoformat(),
                      "provider": saved.provider_id, "active": saved.is_active})
        messages.success(request, "Personal processing schedule saved. No invoices were approved or paid.")
        return redirect(saved)
    return render(request, "core/work_schedule.html", {"form": form, "schedule": schedule,
        "runs": schedule.completions.all()[:30] if schedule else []})


@login_required
@require_POST
def schedule_action(request, pk):
    schedule = get_object_or_404(owned_schedules(request.user), pk=pk)
    try:
        action = request.POST.get("action")
        if action == "complete":
            try:
                due = date.fromisoformat(request.POST.get("due_token", ""))
            except (ValueError, TypeError):
                raise ValidationError("Reload the schedule and select its current processing cycle.") from None
            schedule, created = complete_cycle(request.user, pk, due, request.POST.get("completion_note", ""))
            messages.success(request, "One processing cycle completed." if created else "This cycle was already completed; no duplicate was recorded.")
        elif action == "toggle":
            with transaction.atomic():
                schedule = get_object_or_404(owned_schedules(request.user).select_for_update(), pk=pk)
                if not schedule.is_active and schedule.completions.filter(due_date=schedule.next_due_date).exists():
                    raise ValidationError("Edit the next processing date before resuming a completed schedule.")
                target = request.POST.get("target_active")
                if target not in {"0", "1"}:
                    raise ValidationError("Reload the schedule before changing its active state.")
                schedule.is_active = target == "1"
                schedule.save(update_fields=["is_active", "updated_at"])
                AuditEvent.record("work.schedule_resumed" if schedule.is_active else "work.schedule_paused",
                    obj=schedule, actor=request.user, org_unit_id=schedule.org_unit_id)
            messages.success(request, "Schedule resumed." if schedule.is_active else "Schedule paused. Any underlying overdue bills remain overdue.")
        else:
            raise ValidationError("Choose a valid schedule action.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(reverse("core:my_work") + "?tab=schedules&mode=list")
