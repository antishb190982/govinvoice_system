"""Operational ownership only; never override bank matching or payment evidence."""
import uuid

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import AuditEvent
from core.notify import notify
from org.access import has_role, users_with_role
from reconciliation import bank
from reconciliation.models import BankRow


class CaseForm(forms.Form):
    assigned_to = forms.ModelChoiceField(queryset=None, label="Responsible officer")
    resolution_due_date = forms.DateField(label="Resolution deadline",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    note = forms.CharField(max_length=2000, label="Assignment or progress note",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Explain the next action, reassignment or deadline change. Recorded in the audit trail.")

    def __init__(self, *args, row, **kwargs):
        self.row = row
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = users_with_role(row.statement.org_unit, bank.MANAGERS).filter(
            is_active=True).distinct()
        self.initial.update(assigned_to=row.assigned_to_id, resolution_due_date=row.resolution_due_date)

    def clean_resolution_due_date(self):
        due = self.cleaned_data["resolution_due_date"]
        if due < timezone.localdate() and due != self.row.resolution_due_date:
            raise ValidationError("A new deadline cannot be in the past. Existing overdue deadlines may be retained.")
        return due


@transaction.atomic
def update_case(row, user, owner, due_date, note):
    row = BankRow.objects.select_for_update().select_related("statement__org_unit", "assigned_to").get(pk=row.pk)
    unit = row.statement.org_unit
    if not has_role(user, unit, bank.MANAGERS):
        raise PermissionDenied
    if row.status != BankRow.Status.EXCEPTION:
        raise ValidationError("Only unresolved bank exceptions can be assigned. Recheck the transaction first.")
    if not owner or not owner.is_active or not has_role(owner, unit, bank.MANAGERS):
        raise ValidationError("Choose an active finance officer or unit administrator authorised for this unit.")
    note = note.strip()
    if not note or len(note) > 2000:
        raise ValidationError("Provide a progress or assignment note of up to 2,000 characters.")
    if not due_date or (due_date < timezone.localdate() and due_date != row.resolution_due_date):
        raise ValidationError("Choose a current or future resolution deadline.")
    previous = {"owner": row.assigned_to_id, "due_date": str(row.resolution_due_date or "")}
    row.assigned_to = owner
    row.resolution_due_date = due_date
    row.case_note = note
    row.assigned_by = user
    row.assigned_at = timezone.now()
    row.save(update_fields=["assigned_to", "resolution_due_date", "case_note", "assigned_by", "assigned_at"])
    AuditEvent.record("bank.case_updated", obj=row, actor=user, org_unit_id=unit.pk,
        data={"previous": previous, "owner": owner.pk, "owner_label": owner.get_full_name() or owner.get_username(),
              "due_date": due_date.isoformat(), "note": note})
    notify([owner], "Bank exception assigned — action required",
        f"Statement line {row.line_number}; resolution deadline {due_date.isoformat()}.",
        link=row.get_absolute_url(), dedupe_key=f"bank-case-{row.pk}-{uuid.uuid4().hex}", action_required=True)
    return row
