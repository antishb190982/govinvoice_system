"""Durable, scoped preparation wizard; intake keeps its existing review gates."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from core.models import AuditEvent
from core.utils import require_any_role
from imports.views import UploadForm
from imports.bulk import apply_folder_paths, stage_uploads
from invoices.models import Invoice
from operations.forms import BatchForm
from operations.models import BatchPreparation
from operations.services import PROCESSORS, create_batch, ministry_for
from org.access import has_role
from org.access import can_view


def owned_preparation(user, pk):
    prep = get_object_or_404(BatchPreparation.objects.select_related("org_unit", "provider", "prepared_by"), pk=pk)
    if prep.prepared_by_id != user.pk or not has_role(user, prep.org_unit, PROCESSORS):
        raise PermissionDenied
    return prep


def candidates(prep):
    # Account allocation is authoritative, never the uploader or OCR's guessed ministry.
    return Invoice.objects.filter(org_unit__path__startswith=prep.org_unit.path,
        service_account__org_unit__path__startswith=prep.org_unit.path,
        service_account__provider=prep.provider, provider=prep.provider, currency=prep.currency
    ).exclude(status__in=["paid", "closed_zero", "cancelled", "scheduled"]).exclude(
        batch_memberships__active=True).select_related("service_account", "processing_owner", "org_unit")


@login_required
def prepare_new(request):
    require_any_role(request.user, PROCESSORS)
    initial = request.GET.copy()
    source_id = request.POST.get("source_invoice") if request.method == "POST" else request.GET.get("invoice")
    source_invoice = None
    if source_id:
        source_invoice = get_object_or_404(Invoice.objects.select_related("org_unit", "provider", "service_account"),
                                           pk=source_id)
        if not can_view(request.user, source_invoice.org_unit):
            raise PermissionDenied
        initial.setdefault("provider", source_invoice.provider_id)
        initial.setdefault("currency", source_invoice.currency)
        initial.setdefault("org_unit", ministry_for(source_invoice.org_unit).pk)
    if not initial.get("provider") and getattr(request, "utility_provider", None):
        initial["provider"] = request.utility_provider.pk
    form = BatchForm(request.POST or None, user=request.user, initial=initial)
    del form.fields["invoices"]
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            ministry_for(d["org_unit"])
        except ValidationError as exc:
            form.add_error("org_unit", exc)
        else:
            prep = BatchPreparation.objects.create(name=d["name"], org_unit=d["org_unit"],
                provider=d["provider"], currency=d["currency"], prepared_by=request.user)
            if source_invoice and candidates(prep).filter(pk=source_invoice.pk).exists():
                prep.selected_invoice_ids = [source_invoice.pk]
                prep.save(update_fields=["selected_invoice_ids"])
            AuditEvent.record("batch.preparation_started", obj=prep, actor=request.user,
                org_unit_id=prep.org_unit_id, data={"provider": prep.provider_id})
            return redirect(f"{prep.get_absolute_url()}?step={'confirm' if prep.selected_invoice_ids else 'select'}")
    return render(request, "operations/prepare_setup.html", {"form": form,
        "source_invoice": source_invoice})


@transaction.atomic
def complete(prep, user):
    prep = BatchPreparation.objects.select_for_update().get(pk=prep.pk)
    if prep.completed_batch_id:
        return prep.completed_batch
    if prep.prepared_by_id != user.pk or not has_role(user, prep.org_unit, PROCESSORS):
        raise PermissionDenied
    ids = prep.selected_invoice_ids
    if not ids or set(candidates(prep).filter(pk__in=ids).values_list("pk", flat=True)) != set(ids):
        raise ValidationError("Some invoices changed or entered another batch. Return to invoice selection.")
    batch = create_batch(user, prep.org_unit, prep.name, ids)
    if batch.provider_id != prep.provider_id or batch.currency != prep.currency:
        raise ValidationError("Provider or currency changed; select invoices again.")
    prep.completed_batch = batch
    prep.save(update_fields=["completed_batch"])
    return batch


@login_required
def prepare(request, pk):
    prep = owned_preparation(request.user, pk)
    if prep.completed_batch_id:
        return redirect(prep.completed_batch)
    step = request.GET.get("step", "source")
    if step not in {"source", "select", "confirm"}:
        step = "source"
    upload = UploadForm(request.POST or None, request.FILES or None,
                        initial={"provider": prep.provider_id})
    upload.fields["provider"].queryset = upload.fields["provider"].queryset.filter(pk=prep.provider_id)
    upload.fields["provider"].required = True
    selection = candidates(prep)
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "upload" and upload.is_valid():
                d = upload.cleaned_data
                folder = d["folder_files"]
                apply_folder_paths(folder, d["folder_paths"])
                batches, skipped = stage_uploads(request.user, d["files"] + folder,
                    prep.provider, received_date=d["received_date"])
                prep.imports.add(*batches)
                if request.POST.get("intake_route") == "registration":
                    from imports.registration import registration_url
                    from imports.models import ImportBatch
                    ImportBatch.objects.filter(pk__in=[b.pk for b in batches]).update(registration_mode=True)
                    return redirect(registration_url(batches))
                messages.info(request, "Review and import the staged documents below, then continue to detected invoices.")
                if skipped:
                    messages.warning(request, f"{len(skipped)} files were skipped. Check the intake results.")
                return redirect(prep)
            if action == "select":
                ids = request.POST.getlist("invoices")
                if not ids or any(not i.isdigit() for i in ids):
                    raise ValidationError("Select at least one detected invoice.")
                ids = sorted(set(map(int, ids)))
                if set(selection.filter(pk__in=ids).values_list("pk", flat=True)) != set(ids):
                    raise ValidationError("Select only available invoices for the allocated ministry and provider.")
                prep.selected_invoice_ids = ids
                prep.save(update_fields=["selected_invoice_ids"])
                return redirect(prep.get_absolute_url() + "?step=confirm")
            if action == "complete":
                batch = complete(prep, request.user)
                messages.success(request, f"{batch.reference} prepared. Ministry and preparer ownership recorded.")
                return redirect(batch)
        except ValidationError as exc:
            error = " ".join(exc.messages)
    selected = selection.filter(pk__in=prep.selected_invoice_ids)
    if step == "confirm" and selected.count() != len(prep.selected_invoice_ids):
        error = "Selection changed. Return to invoice selection before creating the batch."
    from providers.models import ServiceAccount
    from django.db.models import Sum
    return render(request, "operations/prepare.html", {
        "prep": prep, "step": step, "upload_form": upload, "candidates": selection,
        "selected": selected, "error": error,
        "selected_total": selected.aggregate(total=Sum("amount_total"))["total"],
        "allocated_accounts": ServiceAccount.objects.filter(provider=prep.provider,
            org_unit__path__startswith=prep.org_unit.path).count(),
        "imports": [{"batch": item, "registration_url": __import__(
            "imports.registration", fromlist=["registration_url"]).registration_url([item])}
            for item in prep.imports.prefetch_related("rows").all()],
    })
