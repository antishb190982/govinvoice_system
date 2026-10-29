"""First-time intake routing; administrators register accounts, clerks import."""

from urllib.parse import parse_qs, urlencode, urlsplit

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import resolve, reverse
from django.utils import timezone

from core.models import AuditEvent, Notification
from core.utils import require_any_role
from imports import services
from imports.models import ImportBatch, ImportRow, RegistrationRequest
from org.access import has_role, unit_filter, units_for, user_has_any_role
from org.models import Membership, OrgUnit, Role
from providers.models import Provider
from providers.services import find_account


class RoutingForm(forms.Form):
    org_unit = forms.ModelChoiceField(queryset=OrgUnit.objects.none(), label="Unit for registration",
                                      help_text="The unit's administrators will receive the request.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user, [Role.CLERK, Role.UNIT_ADMIN])
        self.fields["org_unit"].label_from_instance = lambda unit: unit.indented_name


class ProviderSelectionForm(forms.Form):
    provider = forms.ModelChoiceField(queryset=Provider.objects.filter(is_active=True),
                                      label="Provider for unidentified invoices")


def missing_requirements(batch):
    """Deduplicate missing accounts, but never treat a scope error as a new account."""
    missing, pending = {}, 0
    providers, known_accounts = {}, {}
    for row in batch.rows.filter(include=True).exclude(
            status__in=[ImportRow.Status.COMMITTED, ImportRow.Status.SKIPPED]):
        if row.status == ImportRow.Status.ANALYSING:
            pending += 1
            continue
        provider_id = row.data.get("provider_id")
        if provider_id not in providers:
            providers[provider_id] = Provider.objects.filter(pk=provider_id).first()
        provider = providers[provider_id]
        account_number = row.data.get("account_number") or ""
        service_number = row.data.get("service_number") or ""
        if provider:
            account_key = (provider.pk, account_number, service_number)
            if account_key not in known_accounts:
                known_accounts[account_key] = bool(find_account(provider, account_number, service_number))
            if known_accounts[account_key]:
                continue
        provider_label = provider.name if provider else row.data.get("provider") or "Not identified"
        key = (provider.pk if provider else provider_label, account_number, service_number)
        if key not in missing:
            missing[key] = {"provider": provider, "provider_label": provider_label,
                            "account_number": account_number, "service_number": service_number,
                            "row": row, "count": 0}
        missing[key]["count"] += 1
    return list(missing.values()), pending


def registration_url(batches):
    return reverse("imports:registration") + "?" + urlencode({"batches": ",".join(str(b.pk) for b in batches)})


def registration_blocked(batch):
    if not batch.registration_mode:
        return False
    missing, pending = missing_requirements(batch)
    return bool(missing or pending or RegistrationRequest.objects.filter(
        batch=batch, status=RegistrationRequest.Status.REQUESTED,
    ).exists())


def owned_batches(user, raw):
    try:
        ids = {int(value) for value in (raw or "").split(",")}
        if not ids or len(ids) > 100 or min(ids) < 1:
            raise ValueError
    except (ValueError, TypeError):
        raise Http404 from None
    queryset = ImportBatch.objects.select_related("uploaded_by", "provider").filter(pk__in=ids)
    if not user.is_superuser:
        queryset = queryset.filter(uploaded_by=user)
    batches = list(queryset.order_by("pk"))
    if len(batches) != len(ids):
        raise Http404
    return batches


def visible_requests(user):
    queryset = RegistrationRequest.objects.select_related("batch__uploaded_by", "org_unit", "requested_by")
    if user.is_superuser:
        return queryset
    return queryset.filter(
        Q(requested_by=user) | unit_filter(user, [Role.UNIT_ADMIN])
    ).distinct()


def can_administer(user, item):
    return has_role(user, item.org_unit, [Role.UNIT_ADMIN])


def _notify_administrators(item):
    from accounts.models import User

    ids = set(User.objects.filter(is_active=True, is_superuser=True).values_list("pk", flat=True))
    memberships = Membership.objects.filter(role=Role.UNIT_ADMIN, user__is_active=True).filter(
        Q(valid_until__isnull=True) | Q(valid_until__gte=timezone.localdate())
    ).select_related("org_unit")
    ids.update(m.user_id for m in memberships if item.org_unit.path.startswith(m.org_unit.path))
    for user_id in ids:
        Notification.objects.get_or_create(
            user_id=user_id, dedupe_key=f"intake-registration:{item.pk}:{item.revision}",
            defaults={"title": f"Invoice account registration · {item.org_unit.code}",
                      "body": f"Import #{item.batch_id} needs account registration before invoice review can continue.",
                      "link": reverse("imports:registration_request", args=[item.pk])},
        )


def recheck_registration(batch, actor):
    """Recheck with the uploader's scope, never the administrator's wider scope."""
    if batch.status != ImportBatch.Status.STAGED:
        return
    services.recheck(batch, batch.uploaded_by)
    AuditEvent.record("import.registration_rechecked", obj=batch, actor=actor)
    item = RegistrationRequest.objects.filter(batch=batch).first()
    if item and item.status == RegistrationRequest.Status.REQUESTED and can_administer(actor, item):
        missing, pending = missing_requirements(batch)
        if not missing and not pending:
            item.status = RegistrationRequest.Status.READY
            item.completed_at, item.completed_by = timezone.now(), actor
            item.save(update_fields=["status", "completed_at", "completed_by"])
            Notification.objects.get_or_create(
                user=item.requested_by, dedupe_key=f"intake-registration-ready:{item.pk}:{item.revision}",
                defaults={"title": "Account registration complete",
                          "body": f"Import #{batch.pk} is ready to resume invoice review. No invoices were approved.",
                          "link": reverse("imports:batch", args=[batch.pk])},
            )
            AuditEvent.record("import.registration_completed", obj=batch, actor=actor)


def refresh_return_destination(user, target):
    """Only refresh an explicitly authorized intake route after a master-data POST."""
    try:
        parsed = urlsplit(target or "")
    except ValueError:
        return False
    if parsed.scheme or parsed.netloc:
        return False
    try:
        if parsed.path == reverse("imports:registration"):
            batches = owned_batches(user, parse_qs(parsed.query).get("batches", [""])[0])
            for batch in batches:
                recheck_registration(batch, user)
            return True
        match = resolve(parsed.path)
        if match.view_name == "imports:registration_request":
            item = get_object_or_404(visible_requests(user), pk=match.kwargs["pk"])
            if not can_administer(user, item):
                return False
            recheck_registration(item.batch, user)
            return True
    except (Http404, ValueError):
        return False
    return False


def _links(missing, return_url, user, org_unit=None, item=None):
    admin = has_role(user, org_unit, [Role.UNIT_ADMIN]) if org_unit else \
        user_has_any_role(user, [Role.UNIT_ADMIN])
    for group in missing:
        group["register_url"] = ""
        if group["provider"] and admin:
            params = {"provider": group["provider"].pk, "account_number": group["account_number"],
                      "meter_number": group["service_number"], "next": return_url}
            if org_unit:
                params["org_unit"] = org_unit.pk
            group["register_url"] = reverse("providers:account_create") + "?" + urlencode(params)
        if item:
            group["source_url"] = reverse("imports:registration_source", args=[item.pk, group["row"].pk])
    return missing


@login_required
def registration_hub(request):
    require_any_role(request.user, [Role.CLERK, Role.UNIT_ADMIN])
    batches = owned_batches(request.user, request.GET.get("batches"))
    target = registration_url(batches)
    bound_form, bound_batch, bound_provider, provider_batch = None, None, None, None
    if request.method == "POST":
        batch = next((b for b in batches if str(b.pk) == request.POST.get("batch")), None)
        if not batch or batch.status != ImportBatch.Status.STAGED:
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "recheck":
            recheck_registration(batch, request.user)
            return redirect(target)
        if action == "provider":
            form = ProviderSelectionForm(request.POST, prefix=f"provider-{batch.pk}")
            bound_provider, provider_batch = form, batch.pk
            if form.is_valid():
                batch.provider = form.cleaned_data["provider"]
                batch.save(update_fields=["provider"])
                recheck_registration(batch, request.user)
                return redirect(target)
        elif action == "request":
            bound_batch = batch.pk
            bound_form = RoutingForm(request.POST, user=request.user, prefix=f"batch-{batch.pk}")
            if bound_form.is_valid():
                missing, pending = missing_requirements(batch)
                if not missing and not pending:
                    bound_form.add_error(None, "Accounts are already registered. Continue to invoice review.")
                else:
                    with transaction.atomic():
                        item, created = RegistrationRequest.objects.select_for_update().get_or_create(
                            batch=batch, defaults={"org_unit": bound_form.cleaned_data["org_unit"],
                                                   "requested_by": batch.uploaded_by},
                        )
                        if not created:
                            if item.org_unit_id != bound_form.cleaned_data["org_unit"].pk or \
                                    item.status != RegistrationRequest.Status.REQUESTED:
                                item.revision += 1
                            item.org_unit = bound_form.cleaned_data["org_unit"]
                            item.status = RegistrationRequest.Status.REQUESTED
                            item.completed_at = item.completed_by = None
                            item.save()
                        batch.registration_mode = True
                        batch.save(update_fields=["registration_mode"])
                        _notify_administrators(item)
                        AuditEvent.record("import.registration_requested", obj=batch, actor=request.user,
                                          data={"unit": item.org_unit.code})
                    messages.success(request, "Registration routed to the unit administrators. Your files remain saved.")
                    return redirect(target)
        else:
            raise PermissionDenied("Unknown registration action.")
    bundles = []
    for batch in batches:
        missing, pending = missing_requirements(batch)
        bundles.append({
            "batch": batch, "missing": _links(missing, target, request.user), "pending": pending,
            "routing_form": bound_form if batch.pk == bound_batch else
                RoutingForm(user=request.user, prefix=f"batch-{batch.pk}"),
            "provider_form": bound_provider if batch.pk == provider_batch else
                ProviderSelectionForm(prefix=f"provider-{batch.pk}"),
            "registration_request": RegistrationRequest.objects.filter(batch=batch).first(),
        })
    from providers.views import is_master_data_admin

    return render(request, "imports/registration.html", {
        "bundles": bundles, "return_url": target, "can_create_provider": is_master_data_admin(request.user),
        "registration_batches": ",".join(str(batch.pk) for batch in batches),
        "provider_create_url": reverse("providers:create") + "?" + urlencode({"next": target}),
    })


@login_required
def registration_queue(request):
    require_any_role(request.user, [Role.CLERK, Role.UNIT_ADMIN])
    return render(request, "imports/registration_queue.html", {"requests": visible_requests(request.user)})


@login_required
def registration_request(request, pk):
    item = get_object_or_404(visible_requests(request.user), pk=pk)
    admin = can_administer(request.user, item)
    form = ProviderSelectionForm(request.POST or None, prefix="provider")
    if request.method == "POST":
        if not admin or item.batch.status != ImportBatch.Status.STAGED:
            raise PermissionDenied
        if request.POST.get("action") == "provider":
            if form.is_valid():
                item.batch.provider = form.cleaned_data["provider"]
                item.batch.save(update_fields=["provider"])
                recheck_registration(item.batch, request.user)
                return redirect("imports:registration_request", pk=pk)
        elif request.POST.get("action") == "recheck":
            recheck_registration(item.batch, request.user)
            return redirect("imports:registration_request", pk=pk)
        else:
            raise PermissionDenied
    missing, pending = missing_requirements(item.batch)
    from providers.views import is_master_data_admin

    return render(request, "imports/registration_request.html", {
        "item": item, "missing": _links(missing, request.path, request.user, item.org_unit, item),
        "pending": pending, "can_register": admin, "provider_form": form,
        "can_create_provider": is_master_data_admin(request.user),
        "provider_create_url": reverse("providers:create") + "?" + urlencode({"next": request.path}),
    })


@login_required
def registration_source(request, pk, row_pk):
    item = get_object_or_404(visible_requests(request.user), pk=pk)
    row = get_object_or_404(ImportRow, pk=row_pk, batch=item.batch)
    source = row.source_file or item.batch.file
    if not source:
        raise Http404
    AuditEvent.record("import.registration_source_downloaded", obj=item.batch, actor=request.user,
                      data={"row": row.pk})
    response = FileResponse(source.open("rb"), as_attachment=True,
                            filename=(row.source_name or item.batch.original_name).split("/")[-1])
    response["Cache-Control"] = "private, no-store"
    return response
