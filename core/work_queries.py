"""Personal work and scoped overdue work; no financial state is changed here."""
from django.db.models import Min, Q
from django.urls import reverse
from django.utils import timezone

from core.control import LABELS, deadline, person, sources, work_row
from core.work_schedules import owned_schedules
from imports.models import ImportRow
from invoices.models import Invoice
from operations.models import BatchPreparation, DisputeCase, InvoiceBatch
from org.access import scope
from org.models import Role
from workflow.engine import pending_for


EXTRA_LABELS = {
    "wizards": "Unfinished batch preparations", "returned_batches": "Returned batches",
    "schedules": "Processing schedules", "invoice_anomalies": "Submitted invoice anomalies",
    "import_anomalies": "Submitted file checks", "bank_anomalies": "Your bank exceptions",
    "provider_anomalies": "Your provider mismatches", "dispute_anomalies": "Submitted disputes / adjustments",
    "disputes": "Disputes requiring your role", "invoices": "Overdue invoices",
    "approval_exceptions": "Overdue approvals missing a batch",
}


def apply_scope(qs, data, unit="org_unit", provider="provider"):
    if data.get("org_unit"):
        qs = qs.filter(**{unit + "__path__startswith": data["org_unit"].path})
    if data.get("provider"):
        qs = qs.filter(**{provider: data["provider"]})
    return qs


def schedules_for(user, data):
    without_provider = {**data, "provider": None}
    qs = apply_scope(owned_schedules(user), without_provider)
    if data.get("provider"):
        qs = qs.filter(Q(provider=data["provider"]) | Q(provider__isnull=True))
    return qs


def personal_tasks(user, data):
    items = sources(user, data)
    items["intake"] = items["intake"].filter(uploaded_by=user)
    items["preparation"] = items["preparation"].filter(
        Q(processing_owner=user) | Q(processing_owner__isnull=True, created_by=user)).select_related("created_by")
    items["approval"] = items["approval"].filter(pk__in=pending_for(user).filter(
        invoice__batch_memberships__active=True).values("invoice__batch_memberships__batch_id"))
    items["provider"] = items["provider"].filter(assigned_to=user)
    items["bank"] = items["bank"].filter(assigned_to=user)
    instructions = items["settlement"]
    authorisations = scope(instructions.filter(status="pending"), user, [Role.AUTHORISER]).exclude(prepared_by=user)
    items["settlement"] = instructions.filter(
        Q(prepared_by=user, status__in=["draft", "authorised", "sent"]) | Q(pk__in=authorisations.values("pk")))
    items["wizards"] = apply_scope(BatchPreparation.objects.filter(
        prepared_by=user, completed_batch__isnull=True).select_related("org_unit", "provider", "prepared_by").order_by("pk"), data)
    items["returned_batches"] = apply_scope(scope(InvoiceBatch.objects.filter(created_by=user,
        cancelled_at__isnull=True, members__active=True, members__invoice__status="returned"), user).distinct(
        ).select_related("org_unit", "provider", "created_by").order_by("pk"), data)
    items["schedules"] = schedules_for(user, data).filter(is_active=True, next_due_date__lte=timezone.localdate())
    cases = apply_scope(DisputeCase.objects.select_related("invoice__provider", "org_unit", "opened_by"),
        data, provider="invoice__provider")
    finance = scope(cases.filter(status__in=["open", "refund_due"]), user, [Role.FINANCE, Role.UNIT_ADMIN]).exclude(
        status="open", opened_by=user)
    authority = scope(cases.filter(status="authorisation"), user, [Role.AUTHORISER]).exclude(
        opened_by=user).exclude(finance_by=user)
    items["disputes"] = cases.filter(Q(pk__in=finance.values("pk")) | Q(pk__in=authority.values("pk"))).order_by("pk")
    return items


def anomalies(user, data):
    invoices = scope(Invoice.objects.filter(
        Q(created_by=user) | Q(processing_owner=user) | Q(batch_memberships__batch__created_by=user)),
        user).distinct().select_related("org_unit", "provider", "processing_owner", "created_by")
    invoices = apply_scope(invoices, data)
    imports = ImportRow.objects.filter(batch__uploaded_by=user).filter(
        Q(status="invalid") | ~Q(errors=[]) | Q(needs_review=True, confirmed_at__isnull=True)
    ).select_related("batch__provider", "batch__uploaded_by")
    if data.get("org_unit"):
        from core.staged_scope import staged_unit
        imports = staged_unit(imports, data["org_unit"])
    if data.get("provider"):
        from core.staged_scope import staged_provider
        imports = staged_provider(imports, data["provider"])
    team = sources(user, data)
    cases = scope(DisputeCase.objects.filter(
        Q(opened_by=user) | Q(invoice__in=invoices)), user).select_related("org_unit", "invoice__provider")
    return {
        "invoice_anomalies": invoices.filter(~Q(flags=[]) | Q(status__in=["returned", "disputed"])).order_by("-pk"),
        "import_anomalies": imports.order_by("-batch_id", "row_number"),
        "provider_anomalies": team["provider"].filter(Q(assigned_to=user) | Q(invoice__in=invoices)),
        "bank_anomalies": team["bank"].filter(
            Q(assigned_to=user) | Q(statement__uploaded_by=user) | Q(instruction__prepared_by=user)),
        "dispute_anomalies": apply_scope(cases, data, provider="invoice__provider").order_by("-pk"),
    }


def overdue_sources(user, data):
    today, now = timezone.localdate(), timezone.now()
    team = sources(user, data)
    invoices = apply_scope(scope(Invoice.objects.filter(status__in=Invoice.UNPAID | {Invoice.Status.DRAFT}), user).select_related(
        "org_unit", "provider", "processing_owner", "created_by"), data)
    return {
        "invoices": invoices.filter(Q(due_date__lt=today) | Q(
            status__in=["in_approval", "returned"], must_approve_by__lt=now)).order_by("due_date", "pk"),
        "approval": team["approval"].filter(operation_due__lt=now),
        "approval_exceptions": apply_scope(scope(Invoice.objects.filter(
            approval_tasks__status="pending", approval_tasks__due_at__lt=now), user).exclude(
                batch_memberships__active=True).select_related("org_unit", "provider").annotate(
                    work_due=Min("approval_tasks__due_at", filter=Q(approval_tasks__status="pending"))).distinct().order_by("work_due", "pk"), data),
        "settlement": team["settlement"].filter(Q(pay_by__lt=today) | Q(status="pending", authorise_by__lt=now)),
        "bank": team["bank"].filter(resolution_due_date__lt=today),
        "schedules": schedules_for(user, data).filter(is_active=True, next_due_date__lt=today),
    }


def label(key):
    return EXTRA_LABELS.get(key, LABELS.get(key, key))


def row_for(key, obj):
    original = {"bank_anomalies": "bank", "provider_anomalies": "provider"}.get(key, key)
    if original in LABELS:
        row = work_row(original, obj)
        row["status"], row["detail"] = row.pop("stage"), row.pop("blocker")
        if key == "preparation" and not row["owner"]:
            row["owner"] = person(obj.created_by)
        return row
    row = {"kind": label(key), "title": "", "url": "", "unit": "", "provider": "",
        "owner": "", "status": "", "detail": "", "next_action": "", "due_label": "", "overdue": False}
    due = None
    if key in {"invoices", "invoice_anomalies", "approval_exceptions"}:
        due = obj.work_due if key == "approval_exceptions" else obj.due_date
        row.update(title=obj.reference, url=obj.get_absolute_url(), unit=str(obj.org_unit),
            provider=str(obj.provider), status=obj.get_status_display(),
            owner=person(obj.processing_owner or obj.created_by),
            detail="; ".join(str(flag).replace("_", " ") for flag in obj.flags) or obj.get_status_display(),
            next_action="Inspect invoice checks and processing history")
        if obj.must_approve_by and obj.status in ["in_approval", "returned"] and obj.must_approve_by < timezone.now():
            due = obj.must_approve_by
        if key == "approval_exceptions":
            row.update(detail="An overdue approval has no active batch linkage.",
                next_action="Resolve missing batch linkage; do not approve individually")
    elif key == "import_anomalies":
        row.update(title=obj.source_name or f"Import row {obj.row_number}",
            url=reverse("imports:batch", args=[obj.batch_id]), provider=str(obj.batch.provider or "Not identified"),
            owner=person(obj.batch.uploaded_by),
            status=obj.get_status_display(), detail="; ".join(map(str, obj.errors)) or "Extraction needs a staff check",
            next_action="Review the existing extracted data; resolve account or validation issues")
    elif key == "schedules":
        due = obj.next_due_date
        action_url = reverse("core:work_schedule_action", args=[obj.pk])
        row.update(title=obj.name, url=obj.get_absolute_url(), unit=str(obj.org_unit),
            provider=str(obj.provider or "All providers in unit"), status="Active" if obj.is_active else "Paused",
            owner=person(obj.owner),
            detail=f"{obj.get_frequency_display()} · {obj.notes}", next_action="Complete one work cycle or edit the plan",
            is_schedule=True, active=obj.is_active, complete_url=action_url, toggle_url=action_url,
            due_token=obj.next_due_date.isoformat())
    elif key in {"wizards", "returned_batches"}:
        row.update(title=getattr(obj, "reference", obj.name), url=obj.get_absolute_url(),
            unit=str(obj.org_unit), provider=str(obj.provider), status="Returned for correction" if key == "returned_batches" else "Preparation unfinished",
            owner=person(obj.created_by if key == "returned_batches" else obj.prepared_by),
            next_action="Review correction requests" if key == "returned_batches" else "Continue the preparation wizard")
    else:
        row.update(title=obj.reference, url=obj.get_absolute_url(), unit=str(obj.org_unit),
            provider=str(obj.invoice.provider), status=obj.get_status_display(), detail=obj.reason,
            owner="Authoriser role" if obj.status == "authorisation" else "Finance / case workflow role",
            next_action="Review dispute or adjustment evidence")
    row["due_label"], row["overdue"] = deadline(due)
    if key == "invoice_anomalies" and obj.status not in Invoice.UNPAID | {Invoice.Status.DRAFT}:
        row["overdue"] = False
    if key == "schedules" and not obj.is_active:
        row["overdue"] = False
    return row
