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
        fields = ["label", "destination", "org_unit", "provider", "notes"]
        labels = {"destination": "GovBills folder", "org_unit": "Ministry / unit"}

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


def folder_link(folder, user):
    available = ((not folder.org_unit_id or units_for(user).filter(pk=folder.org_unit_id).exists())
        and (not folder.provider_id or available_providers(user).filter(pk=folder.provider_id).exists()))
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
                                         "provider": saved.provider_id})
        messages.success(request, "Folder linked to your space." if not pk else "Folder link updated.")
        return redirect(reverse("core:my_space") + "?tab=folders")
    return render(request, "core/space_folder.html", {"form": form, "folder": folder})


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
