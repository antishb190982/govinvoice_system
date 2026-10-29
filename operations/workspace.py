"""Read-only team folders, derived from authoritative invoice/payment states."""
from django.contrib.auth.decorators import login_required
from django.db.models import Case, CharField, Count, Exists, OuterRef, Q, Value, When
from django.shortcuts import render

from core.utils import paginate
from imports.models import ImportBatch
from invoices.models import Invoice
from operations.models import BatchMember, DisputeCase, InvoiceBatch
from org.access import scope
from settlements.models import SettlementInstruction

FOLDERS = {
    "preparation": "Preparation / invoices received",
    "work": "Work in progress",
    "rejected": "Rejected / returned",
    "completed": "Completed",
    "all": "All batches",
}


def batches_for(user):
    members = BatchMember.objects.filter(batch_id=OuterRef("pk"), active=True)
    settlements = SettlementInstruction.objects.filter(
        Q(batch_id=OuterRef("pk")) | Q(batch_links__batch_id=OuterRef("pk")))
    return scope(InvoiceBatch.objects.select_related(
        "provider", "ministry", "org_unit", "created_by"), user).annotate(
        has_members=Exists(members),
        has_unpaid=Exists(members.exclude(invoice__status=Invoice.Status.PAID)),
        has_returned=Exists(members.filter(invoice__status=Invoice.Status.RETURNED)),
        has_progress=Exists(members.exclude(invoice__status=Invoice.Status.DRAFT)),
        has_executed=Exists(settlements.filter(status="executed")),
        has_open_dispute=Exists(DisputeCase.objects.filter(
            invoice__batch_memberships__batch_id=OuterRef("pk"),
            invoice__batch_memberships__active=True,
            status__in=["open", "authorisation", "refund_due"])),
        has_returned_settlement=Exists(settlements.filter(
            status="draft").exclude(rejection_comment="")),
    ).annotate(batch_folder=Case(
        When(Q(cancelled_at__isnull=False) | Q(has_returned=True) |
             Q(has_returned_settlement=True), then=Value("rejected")),
        When(has_open_dispute=True, then=Value("work")),
        When(Q(has_members=False) | Q(has_unpaid=False) | Q(has_executed=True),
             then=Value("completed")),
        When(started_at__isnull=True, has_progress=False, then=Value("preparation")),
        default=Value("work"), output_field=CharField(),
    ))


def counts_for(user):
    counts = dict.fromkeys(FOLDERS, 0)
    for row in batches_for(user).order_by().values("batch_folder").annotate(total=Count("pk")):
        counts[row["batch_folder"]] = row["total"]
    counts["all"] = sum(counts[key] for key in FOLDERS if key != "all")
    return counts


@login_required
def workspace(request):
    folder = request.GET.get("folder", "preparation")
    if folder not in FOLDERS:
        folder = "preparation"
    qs = batches_for(request.user)
    counts = counts_for(request.user)
    request.batch_folder_counts = counts
    if folder != "all":
        qs = qs.filter(batch_folder=folder)
    q = request.GET.get("q", "").strip()[:120]
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(name__icontains=q) |
                       Q(provider__name__icontains=q))
    received = scope(Invoice.objects.select_related("provider", "org_unit"), request.user).annotate(
        in_batch=Exists(BatchMember.objects.filter(invoice_id=OuterRef("pk"), active=True))
    ).filter(in_batch=False, status=Invoice.Status.RETURNED if folder == "rejected" else Invoice.Status.DRAFT)
    if folder not in ("preparation", "rejected"):
        received = received.none()
    if q:
        received = received.filter(Q(reference__icontains=q) | Q(provider__name__icontains=q))
    # Imports do not have an organisation FK until their rows are registered:
    # only expose the current uploader's staged source files here.
    imports = ImportBatch.objects.filter(uploaded_by=request.user, status=ImportBatch.Status.STAGED)
    if folder != "preparation":
        imports = imports.none()
    if q:
        imports = imports.filter(original_name__icontains=q)
    return render(request, "operations/workspace.html", {
        "folder": folder, "folder_labels": FOLDERS, "folder_counts": counts,
        "page": paginate(request, qs), "q": q,
        "received": received.order_by("-pk")[:30], "received_count": received.count(),
        "imports": imports[:20], "import_count": imports.count(),
    })
