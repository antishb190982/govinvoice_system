from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from core.models import AuditEvent
from core.utils import csv_response, paginate, require_any_role
from imports import parsers, services
from imports.bulk import apply_folder_paths, stage_uploads
from imports.models import ImportBatch, ImportRow
from org.models import Role
from providers.models import Provider


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data]
        return [single(data, initial)] if data else []


class UploadForm(forms.Form):
    provider = forms.ModelChoiceField(queryset=Provider.objects.filter(is_active=True), required=False,
                                      empty_label="Detect from the file",
                                      help_text="Choose the provider unless the file has a provider column.")
    received_date = forms.DateField(initial=timezone.localdate,
                                    widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                                    help_text="Actual receipt date. Spreadsheet receipt dates take precedence when provided.")
    files = MultipleFileField(required=False, label="Files or ZIP archives",
                              help_text="Mix spreadsheets, PDFs, scans and ZIPs; up to 100 invoice files / 100 MB.")
    folder_files = MultipleFileField(required=False, label="Or select a whole folder",
                                    widget=MultipleFileInput(attrs={"webkitdirectory": True, "directory": True,
                                                                   "data-folder-files": True}),
                                    help_text="Includes subfolders. Use ZIP if your browser cannot select folders.")
    folder_paths = forms.CharField(required=False, widget=forms.HiddenInput)


@login_required
def batch_list(request):
    require_any_role(request.user, [Role.CLERK, Role.UNIT_ADMIN])
    qs = ImportBatch.objects.filter(uploaded_by=request.user) if not request.user.is_superuser else ImportBatch.objects.all()
    from core.provider_scope import filter_provider
    qs = filter_provider(qs)
    if request.GET.get("status") in ImportBatch.Status.values:
        qs = qs.filter(status=request.GET["status"])
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        files = form.cleaned_data["files"]
        folder_files = form.cleaned_data["folder_files"]
        provider = form.cleaned_data["provider"]
        try:
            apply_folder_paths(folder_files, form.cleaned_data["folder_paths"])
            batches, skipped = stage_uploads(
                request.user, files + folder_files, provider, received_date=form.cleaned_data["received_date"]
            )
            if request.POST.get("intake_route") == "registration":
                from imports.registration import registration_url

                ImportBatch.objects.filter(pk__in=[b.pk for b in batches]).update(registration_mode=True)
                return redirect(registration_url(batches))
            if len(batches) == 1 and not skipped:
                return redirect("imports:batch", pk=batches[0].pk)
            from imports.registration import registration_url

            return render(request, "imports/intake_result.html", {
                "batches": batches, "skipped": skipped, "registration_url": registration_url(batches),
            })
        except ValidationError as exc:
            form.add_error("files", exc)
    return render(request, "imports/list.html", {"form": form, "page": paginate(request, qs.select_related("provider"))})


@login_required
@require_POST
def batch_analyse(request, pk):
    batch = _get_batch(request, pk)
    if batch.status != ImportBatch.Status.STAGED:
        raise PermissionDenied
    rows = list(batch.rows.filter(status=ImportRow.Status.ANALYSING).select_related("ocr_document")[:3])
    for row in rows:
        services.apply_ocr(row)
    messages.success(request, f"Analysed {len(rows)} documents. Remaining documents stay safely queued.")
    from imports.registration import registration_url

    if request.POST.get("return_to") == "registration":
        from imports.registration import owned_batches

        if request.POST.get("registration_batches"):
            batches = owned_batches(request.user, request.POST["registration_batches"])
            if not any(item.pk == batch.pk for item in batches):
                raise PermissionDenied
            return redirect(registration_url(batches))
        return redirect(registration_url([batch]))
    return redirect("imports:batch", pk=pk)


def _get_batch(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk)
    if batch.uploaded_by_id != request.user.pk and not request.user.is_superuser:
        raise Http404
    return batch


class RowForm(forms.Form):
    def __init__(self, *args, row=None, **kwargs):
        super().__init__(*args, **kwargs)
        for field in parsers.FIELDS:
            self.fields[field] = forms.CharField(required=False, initial=(row.raw.get(field) if row else ""),
                                                 label=field.replace("_", " ").capitalize())


@login_required
def batch_detail(request, pk):
    batch = _get_batch(request, pk)
    rows = batch.rows.select_related("invoice")
    counts = {s: rows.filter(status=s).count() for s in ImportRow.Status.values}
    required = ("provider", "account_number", "invoice_number", "issue_date", "due_date", "amount_total")
    rows = list(rows.select_related("ocr_document"))
    for r in rows:
        values = [r.confidence.get(f) for f in required if r.confidence]
        r.min_confidence = int(min(v or 0 for v in values) * 100) if values else None
    review_needed = sum(1 for r in rows if r.needs_review and not r.confirmed_at and r.status == ImportRow.Status.VALID)
    edit_row = None
    if request.GET.get("edit"):
        edit_row = get_object_or_404(ImportRow, batch=batch, pk=request.GET["edit"])
    from imports.registration import registration_blocked
    from imports.registration import registration_url

    return render(request, "imports/batch.html", {
        "batch": batch, "rows": rows, "counts": counts, "edit_row": edit_row, "review_needed": review_needed,
        "preparation": batch.preparations.filter(prepared_by=request.user, completed_batch__isnull=True).first(),
        "registration_blocked": registration_blocked(batch) if batch.status == ImportBatch.Status.STAGED else False,
        "registration_url": registration_url([batch]),
        "row_form": RowForm(row=edit_row) if edit_row else None,
    })


@login_required
@require_POST
def batch_row(request, pk, row_pk):
    batch = _get_batch(request, pk)
    row = get_object_or_404(ImportRow, batch=batch, pk=row_pk)
    if "toggle" in request.POST:
        if batch.status != ImportBatch.Status.STAGED:
            raise PermissionDenied
        row.include = not row.include
        row.save(update_fields=["include"])
    else:
        form = RowForm(request.POST, row=row)
        if form.is_valid():
            services.update_row(row, request.user, form.cleaned_data)
            messages.success(request, f"Row {row.row_number} updated.")
    return redirect("imports:batch", pk=batch.pk)


@login_required
@require_POST
def batch_commit(request, pk):
    batch = _get_batch(request, pk)
    try:
        created, failed = services.commit(batch, request.user)
        messages.success(request, f"{len(created)} invoices imported as drafts." + (f" {failed} rows failed." if failed else ""))
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("imports:batch", pk=batch.pk)


@login_required
@require_POST
def batch_recheck(request, pk):
    batch = _get_batch(request, pk)
    changed = services.recheck(batch, request.user)
    messages.success(request, f"Rows re-checked ({changed} changed).")
    return redirect("imports:batch", pk=batch.pk)


@login_required
@require_POST
def batch_discard(request, pk):
    batch = _get_batch(request, pk)
    if batch.status == ImportBatch.Status.STAGED:
        batch.status = ImportBatch.Status.DISCARDED
        batch.save(update_fields=["status"])
        AuditEvent.record("import.discarded", obj=batch)
    return redirect("imports:list")


@login_required
def template_csv(request):
    example = {
        "account_number": "1100453201", "invoice_number": "CEB/2609/0001",
        "issue_date": "2026-10-01", "due_date": "2026-10-31",
        "period_start": "2026-09-01", "period_end": "2026-09-30",
        "amount_net": "10000.00", "amount_tax": "1500.00", "amount_total": "11500.00",
        "consumption": "1250", "previous_reading": "40100", "current_reading": "41350",
        "currency": "MUR", "provider": "CEB", "received_date": timezone.localdate().isoformat(),
    }
    return csv_response("invoice-import-template.csv", parsers.FIELDS,
                        [[example.get(field, "") for field in parsers.FIELDS]])
