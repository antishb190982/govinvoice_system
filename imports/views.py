from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import AuditEvent
from core.utils import csv_response, paginate, require_any_role
from imports import parsers, services
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
    files = MultipleFileField(label="Files", help_text="One spreadsheet (XLSX, XLS, CSV) or up to 100 PDF invoices.")


@login_required
def batch_list(request):
    require_any_role(request.user, [Role.CLERK, Role.UNIT_ADMIN])
    qs = ImportBatch.objects.filter(uploaded_by=request.user) if not request.user.is_superuser else ImportBatch.objects.all()
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        files = form.cleaned_data["files"]
        provider = form.cleaned_data["provider"]
        try:
            names = [f.name.lower() for f in files]
            if all(n.endswith(".pdf") for n in names):
                batch = services.stage_pdfs(request.user, files, provider)
            elif len(files) == 1:
                batch = services.stage_spreadsheet(request.user, files[0], provider)
            else:
                raise ValidationError("Upload either one spreadsheet or a set of PDFs.")
            return redirect("imports:batch", pk=batch.pk)
        except ValidationError as exc:
            form.add_error("files", exc)
    return render(request, "imports/list.html", {"form": form, "page": paginate(request, qs.select_related("provider"))})


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
    edit_row = None
    if request.GET.get("edit"):
        edit_row = get_object_or_404(ImportRow, batch=batch, pk=request.GET["edit"])
    return render(request, "imports/batch.html", {
        "batch": batch, "rows": rows, "counts": counts, "edit_row": edit_row,
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
def batch_discard(request, pk):
    batch = _get_batch(request, pk)
    if batch.status == ImportBatch.Status.STAGED:
        batch.status = ImportBatch.Status.DISCARDED
        batch.save(update_fields=["status"])
        AuditEvent.record("import.discarded", obj=batch)
    return redirect("imports:list")


@login_required
def template_csv(request):
    return csv_response("invoice-import-template.csv", parsers.FIELDS,
                        [["100200300", "INV-2026-0001", "2026-10-01", "2026-10-31", "2026-09-01", "2026-09-30",
                          "100.000", "5.000", "105.000", "1250", "40100", "41350", "OMR", "NAMA-E"]])
