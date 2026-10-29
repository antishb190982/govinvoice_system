from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import AuditEvent, SpaceFolder
from core.provider_scope import available_providers
from org.access import units_for


class SpaceFolderForm(forms.ModelForm):
    class Meta:
        model = SpaceFolder
        fields = ["label", "destination", "org_unit", "provider", "notes", "is_pinned"]
        labels = {"destination": "GovBills folder", "org_unit": "Ministry / unit",
                  "is_pinned": "Pin to left navigation"}

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        self.fields["org_unit"].empty_label = "All my authorised units"
        self.fields["provider"].queryset = available_providers(user)
        self.fields["provider"].empty_label = "All providers"

    def clean_label(self):
        value = self.cleaned_data["label"].strip()
        if SpaceFolder.objects.filter(owner=self.user, label__iexact=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("You already have a folder with this name.")
        return value


def folder_links(folders, user):
    """Resolve a set of personal shortcuts without a permission query per folder."""
    folders = list(folders)
    unit_ids = set(units_for(user).filter(pk__in={f.org_unit_id for f in folders if f.org_unit_id})
                   .values_list("pk", flat=True)) if any(f.org_unit_id for f in folders) else set()
    provider_ids = set(available_providers(user).filter(pk__in={f.provider_id for f in folders if f.provider_id})
                       .values_list("pk", flat=True)) if any(f.provider_id for f in folders) else set()
    return [folder_link(folder, user, unit_ids=unit_ids, provider_ids=provider_ids) for folder in folders]


def folder_link(folder, user, *, unit_ids=None, provider_ids=None):
    unit_ok = not folder.org_unit_id or (
        folder.org_unit_id in unit_ids if unit_ids is not None else
        units_for(user).filter(pk=folder.org_unit_id).exists())
    provider_ok = not folder.provider_id or (
        folder.provider_id in provider_ids if provider_ids is not None else
        available_providers(user).filter(pk=folder.provider_id).exists())
    available = unit_ok and provider_ok
    params = {"tab": folder.destination if folder.destination in {"invoices", "services"} else "batches"}
    if params["tab"] == "batches":
        params["folder"] = folder.destination
    if folder.org_unit_id:
        params["org_unit"] = folder.org_unit_id
    if folder.provider_id:
        params["provider"] = folder.provider_id
    else:
        params["clear_provider"] = "1"
    return {"pk": folder.pk, "label": folder.label,
        "is_pinned": folder.is_pinned,
        "pin_url": reverse("core:space_folder_pin", args=[folder.pk]),
        "url": reverse("core:my_space") + "?" + urlencode(params) if available else "",
        "description": (folder.notes or folder.get_destination_display()) if available else "Access changed. Edit or remove this shortcut.",
        "edit_url": reverse("core:space_folder_edit", args=[folder.pk]),
        "remove_url": reverse("core:space_folder_remove", args=[folder.pk])}


@login_required
def folder_form(request, pk=None):
    folder = get_object_or_404(SpaceFolder, owner=request.user, pk=pk) if pk else None
    form = SpaceFolderForm(request.POST if request.method == "POST" else None, instance=folder, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            saved.owner = request.user
            saved.save()
            AuditEvent.record("space.folder_updated" if pk else "space.folder_linked", obj=saved,
                actor=request.user, data={"destination": saved.destination, "unit": saved.org_unit_id,
                                         "provider": saved.provider_id, "is_pinned": saved.is_pinned})
        messages.success(request, "Folder linked to your space." if not pk else "Folder link updated.")
        return redirect(reverse("core:my_space") + "?tab=folders")
    return render(request, "core/space_folder.html", {"form": form, "folder": folder})


@login_required
@require_POST
def folder_pin(request, pk):
    value = request.POST.get("pinned")
    with transaction.atomic():
        folder = get_object_or_404(SpaceFolder.objects.select_for_update(), owner=request.user, pk=pk)
        if value not in {"0", "1"}:
            messages.error(request, "Choose Pin or Unpin for this folder.")
        elif value == "1" and not folder_link(folder, request.user)["url"]:
            messages.error(request, "This folder is no longer available. Update its filters before pinning.")
        else:
            pinned = value == "1"
            if folder.is_pinned != pinned:
                folder.is_pinned = pinned
                folder.save(update_fields=["is_pinned", "updated_at"])
                AuditEvent.record("space.folder_pinned" if pinned else "space.folder_unpinned",
                                  obj=folder, actor=request.user)
            messages.success(request, "Folder pinned to your navigation." if pinned else
                             "Folder unpinned. Its linked records are unchanged.")
    return redirect(reverse("core:my_space") + "?tab=folders")


@login_required
@require_POST
def folder_remove(request, pk):
    if request.POST.get("confirm") != "1":
        messages.error(request, "Confirm removal of this shortcut. Linked records will not be deleted.")
        return redirect(reverse("core:my_space") + "?tab=folders")
    with transaction.atomic():
        folder = get_object_or_404(SpaceFolder.objects.select_for_update(), owner=request.user, pk=pk)
        AuditEvent.record("space.folder_unlinked", obj=folder, actor=request.user,
            data={"destination": folder.destination})
        folder.delete()
    messages.success(request, "Shortcut removed. No invoices, batches or source folders were deleted.")
    return redirect(reverse("core:my_space") + "?tab=folders")
