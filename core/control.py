"""Scoped operational queues. Counts and drill-through use the same querysets."""
from datetime import datetime

from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import F, Min, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from core.utils import paginate
from core.provider_scope import available_providers
from imports.models import ImportBatch
from invoices.models import Invoice
from operations.models import InvoiceBatch
from org.access import scope, units_for
from org.models import Role
from providers.models import Provider
from reconciliation import bank
from reconciliation.bank_views import rows_for
from reconciliation.models import StatementRow
from settlements.models import SettlementInstruction
from workflow.engine import pending_for
from workflow.models import ApprovalTask


class ControlFilter(forms.Form):
    org_unit = forms.ModelChoiceField(queryset=None, required=False, label="Ministry / unit",
        empty_label="All my units")
    provider = forms.ModelChoiceField(queryset=Provider.objects.filter(is_active=True), required=False,
        empty_label="All utility providers")
    assignment = forms.ChoiceField(required=False, label="Exception owner", choices=[
        ("", "All exception owners"), ("mine", "Assigned to me"), ("unassigned", "Unassigned / inactive")],
        help_text="Applies to provider and bank exceptions only.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        self.fields["provider"].queryset = available_providers(user)


def person(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def deadline(value):
    if not value:
        return "", False
    if isinstance(value, datetime):
        return date_format(timezone.localtime(value), "d M Y, H:i"), value < timezone.now()
    return date_format(value, "d M Y"), value < timezone.localdate()


def sources(user, data):
    processors = [Role.CLERK, Role.UNIT_ADMIN, Role.FINANCE]
    invoices = scope(Invoice.objects.filter(status__in=["draft", "returned"]), user, processors).exclude(
        batch_memberships__active=True).select_related("org_unit", "provider", "processing_owner").order_by("due_date", "pk")
    intake = ImportBatch.objects.filter(status="staged")
    if not user.is_superuser:
        intake = intake.filter(uploaded_by=user)
    intake = intake.select_related("provider", "uploaded_by").order_by("pk")
    team_tasks = scope(ApprovalTask.objects.filter(status="pending"), user, bank.READERS,
        prefix="invoice__org_unit__")
    tasks = ApprovalTask.objects.filter(Q(pk__in=pending_for(user).values("pk")) |
        Q(pk__in=team_tasks.values("pk")), status="pending", invoice__batch_memberships__active=True)
    approval = scope(InvoiceBatch.objects.filter(pk__in=tasks.values(
        "invoice__batch_memberships__batch_id"), cancelled_at__isnull=True), user).select_related(
        "org_unit", "provider").annotate(operation_due=Min("members__invoice__approval_tasks__due_at",
            filter=Q(members__active=True, members__invoice__approval_tasks__status="pending"))).order_by("operation_due", "pk")
    provider = scope(StatementRow.objects.filter(status="unsorted", statement__is_active=True),
        user, bank.READERS + [Role.CLERK], prefix="assigned_unit__").select_related(
            "statement__provider", "assigned_unit", "assigned_to").order_by("pk")
    bank_rows = rows_for(user).filter(status="exception").select_related("assigned_to").order_by(
        F("resolution_due_date").asc(nulls_last=True), "pk")
    settlement = scope(SettlementInstruction.objects.filter(status__in=["draft", "pending", "authorised", "sent"]),
        user, bank.READERS).select_related("org_unit").prefetch_related("lines__provider").order_by("pay_by", "pk")
    unit, utility = data.get("org_unit"), data.get("provider")
    if unit:
        path = unit.path
        invoices = invoices.filter(org_unit__path__startswith=path)
        from core.staged_scope import staged_unit
        intake = staged_unit(intake, unit, batches=True)
        approval = approval.filter(org_unit__path__startswith=path)
        provider = provider.filter(assigned_unit__path__startswith=path)
        bank_rows = bank_rows.filter(statement__org_unit__path__startswith=path)
        settlement = settlement.filter(org_unit__path__startswith=path)
    if utility:
        invoices = invoices.filter(provider=utility)
        from core.staged_scope import staged_provider
        intake = staged_provider(intake, utility, batches=True)
        approval = approval.filter(provider=utility)
        provider = provider.filter(statement__provider=utility)
        bank_rows = bank_rows.filter(Q(provider_code__iexact=utility.short_code) |
            Q(instruction__lines__provider=utility)).distinct()
        settlement = settlement.filter(lines__provider=utility).distinct()
    assignment = data.get("assignment")
    if assignment:
        condition = Q(assigned_to=user) if assignment == "mine" else (
            Q(assigned_to__isnull=True) | Q(assigned_to__is_active=False))
        provider, bank_rows = provider.filter(condition), bank_rows.filter(condition)
    return {"intake": intake, "preparation": invoices, "approval": approval,
            "provider": provider, "bank": bank_rows, "settlement": settlement}


LABELS = {"intake": "Files awaiting review", "preparation": "Invoices awaiting a batch",
          "approval": "Batches awaiting approval", "provider": "Provider reconciliation exceptions",
          "bank": "Bank exceptions", "settlement": "Open settlement instructions"}


def work_row(key, obj):
    row = {"kind": LABELS[key], "title": "", "url": "", "unit": "", "provider": "",
           "owner": "", "stage": "", "blocker": "", "next_action": "", "due_label": "", "overdue": False}
    due = None
    if key == "intake":
        row.update(title=obj.original_name, url=reverse("imports:batch", args=[obj.pk]),
            unit="Awaiting invoice allocation", provider=str(obj.provider or "Not yet identified"),
            owner=person(obj.uploaded_by), stage="Staged — not imported", blocker="Document/account review outstanding",
            next_action="Review staged files and resolve registration")
    elif key == "preparation":
        due = obj.due_date
        row.update(title=obj.reference, url=obj.get_absolute_url(), unit=str(obj.org_unit), provider=str(obj.provider),
            owner=person(obj.processing_owner), stage=obj.get_status_display(), blocker="Not in an active batch",
            next_action="Prepare a batch or resolve returned invoice details")
    elif key == "approval":
        due = obj.operation_due
        row.update(title=obj.reference, url=obj.get_absolute_url(),
            unit=str(obj.org_unit), provider=str(obj.provider), owner="Assigned approval role",
            stage="Awaiting batch decision", blocker="Approval decision outstanding",
            next_action="Review the batch and record a decision")
    elif key == "provider":
        row.update(title=f"Provider statement · line {obj.line_number}", url=obj.get_absolute_url(),
            unit=str(obj.assigned_unit), provider=str(obj.statement.provider), owner=person(obj.assigned_to),
            stage="Unsorted mismatch", blocker=obj.reason or "Provider listing does not reconcile",
            next_action="Open mismatch, assign or correct with evidence")
    elif key == "bank":
        due = obj.resolution_due_date
        row.update(title=f"Bank statement {obj.statement_id} · line {obj.line_number}", url=obj.get_absolute_url(),
            unit=str(obj.statement.org_unit), provider=obj.provider_code or "Not classified",
            owner=person(obj.assigned_to), stage="Needs investigation", blocker=obj.reason or obj.parse_error or "Unmatched bank debit",
            next_action="Assign an officer and deadline" if not obj.assigned_to_id else "Investigate and recheck bank evidence")
        if obj.assigned_to and not obj.assigned_to.is_active:
            row.update(owner="", blocker="Assigned officer is inactive; reassign this exception")
    else:
        due = obj.pay_by
        row.update(title=obj.reference, url=obj.get_absolute_url(), unit=str(obj.org_unit),
            provider=", ".join(sorted({str(line.provider) for line in obj.lines.all()})) or "No transfer lines",
            owner="Authoriser role" if obj.status == "pending" else "Finance team", stage=obj.get_status_display(),
            blocker="Settlement lifecycle is incomplete", next_action="Inspect instruction and required finance action")
    if key == "settlement" and obj.status == "pending" and obj.authorise_by and obj.authorise_by < timezone.now():
        due = obj.authorise_by
        row["blocker"] = "Independent authorisation deadline has passed"
    if key == "preparation" and obj.status == "returned" and obj.must_approve_by and obj.must_approve_by < timezone.now():
        due = obj.must_approve_by
        row["blocker"] = "Returned invoice has missed its approval deadline"
    row["due_label"], row["overdue"] = deadline(due)
    if key == "provider" and obj.assigned_to and not obj.assigned_to.is_active:
        row.update(owner="", blocker="Assigned officer is inactive; reassign this exception")
    return row


@login_required
def control_centre(request):
    params = request.GET.copy()
    if "provider" in params and not params["provider"] and getattr(request, "utility_provider", None):
        params["clear_provider"] = "1"
        return redirect(reverse("core:control_centre") + "?" + params.urlencode())
    if "provider" not in params and getattr(request, "utility_provider", None):
        params["provider"] = request.utility_provider.pk
        return redirect(reverse("core:control_centre") + "?" + params.urlencode())
    form = ControlFilter(params, user=request.user)
    active = params.get("queue", "")
    if active and active not in LABELS:
        raise Http404("Unknown operational queue")
    context = {"filter_form": form, "cards": [], "rows": [], "page": None,
               "active_queue": active, "total_count": 0, "now": timezone.now()}
    if form.is_valid():
        queues = sources(request.user, form.cleaned_data)
        counts = {}
        for key, qs in queues.items():
            count = counts[key] = qs.count()
            unassigned = 0
            if key in {"bank", "provider"}:
                unassigned = qs.filter(Q(assigned_to__isnull=True) | Q(assigned_to__is_active=False)).count()
            elif key == "preparation":
                unassigned = qs.filter(processing_owner__isnull=True).count()
            query = params.copy()
            query.pop("page", None)
            query["queue"] = key
            context["cards"].append({"key": key, "label": LABELS[key], "count": count,
                "unassigned": unassigned, "url": reverse("core:control_centre") + "?" + query.urlencode()})
            field = {"preparation": "due_date", "approval": "operation_due",
                     "bank": "resolution_due_date", "settlement": "pay_by"}.get(key)
            context["cards"][-1]["overdue"] = qs.filter(**{
                field + "__lt": timezone.now() if key == "approval" else timezone.localdate()}).count() if field else 0
        if active:
            page = paginate(request, queues[active], 30)
            page.object_list = [work_row(active, obj) for obj in page.object_list]
            context.update(page=page, rows=page.object_list, total_count=counts[active])
        else:
            rows = [work_row(key, obj) for key, qs in queues.items() for obj in qs[:5]]
            rows.sort(key=lambda row: not row["overdue"])
            context.update(rows=rows, total_count=sum(counts.values()))
    return render(request, "core/control_centre.html", context, status=200 if not form.errors else 400)
