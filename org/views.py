from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from urllib.parse import urlencode

from core.models import AuditEvent
from invoices.models import Invoice
from org.access import can_view, has_role, units_for, scope
from org.models import OrgUnit, Role


class OrgUnitForm(forms.ModelForm):
    class Meta:
        model = OrgUnit
        fields = ["parent", "kind", "code", "name", "name_local", "budget_reference", "is_active"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = units_for(user, [Role.UNIT_ADMIN])
        if self.instance.pk:
            allowed = allowed.exclude(path__startswith=self.instance.path)
        self.fields["parent"].queryset = allowed
        self.fields["parent"].required = not user.is_superuser
        self.fields["parent"].label_from_instance = lambda u: u.indented_name


@login_required
def tree(request):
    from providers.models import ServiceAccount
    provider = getattr(request, "utility_provider", None)
    account_filter = Q(service_accounts__provider=provider) if provider else Q()
    invoice_filter = Q(invoices__provider=provider) if provider else Q()
    units = units_for(request.user).annotate(
        accounts=Count("service_accounts", filter=account_filter, distinct=True),
        unpaid_count=Count("invoices", filter=Q(invoices__status__in=Invoice.UNPAID) & invoice_filter, distinct=True),
    ).order_by("path")
    root = reverse("org:tree")
    params = {"provider": provider.pk} if provider else {}
    def link(route, **extra):
        values = {**params, **extra}
        return route + ("?" + urlencode(values) if values else "")
    if request.GET.get("active") == "1":
        units = units.filter(is_active=True)
    cards = [
        {"label": "Accessible units", "count": units_for(request.user).count(), "url": link(root, view="records")},
        {"label": "Active units", "count": units_for(request.user).filter(is_active=True).count(),
         "url": link(root, view="records", active="1")},
        {"label": "Allocated services", "count": scope(ServiceAccount.objects.all(), request.user).count(),
         "url": link(reverse("core:section_overview", args=["accounts"]))},
        {"label": "Unpaid invoices", "count": scope(Invoice.objects.filter(status__in=Invoice.UNPAID), request.user).count(),
         "url": link(reverse("invoices:list"), status="open")},
    ]
    return render(request, "org/tree.html", {"units": units, "org_dashboard_cards": cards,
        "org_view": "records" if request.GET.get("view") == "records" else "overview",
        "org_overview_url": link(root, view="overview"), "org_records_url": link(root, view="records")})


@login_required
def unit_detail(request, pk):
    unit = get_object_or_404(OrgUnit, pk=pk)
    if not can_view(request.user, unit):
        raise Http404
    return render(request, "org/detail.html", {
        "unit": unit, "ancestors": unit.ancestors(include_self=False),
        "children": unit.children.all(), "memberships": unit.memberships.select_related("user"),
        "accounts": unit.service_accounts.select_related("provider"),
        "can_admin": has_role(request.user, unit, [Role.UNIT_ADMIN]),
    })


@login_required
def unit_form(request, pk=None):
    unit = get_object_or_404(OrgUnit, pk=pk) if pk else None
    if unit and not has_role(request.user, unit, [Role.UNIT_ADMIN]):
        raise PermissionDenied
    form = OrgUnitForm(request.POST or None, instance=unit, user=request.user,
                       initial={"parent": request.GET.get("parent")})
    if request.method == "POST" and form.is_valid():
        parent = form.cleaned_data["parent"]
        if parent is None and not request.user.is_superuser:
            raise PermissionDenied
        if parent is not None and not has_role(request.user, parent, [Role.UNIT_ADMIN]):
            raise PermissionDenied
        changed = form.changed_data
        unit = form.save()
        AuditEvent.record("org_unit.saved", obj=unit, org_unit_id=unit.pk, data={"fields": changed})
        messages.success(request, "Organisation unit saved.")
        return redirect("org:detail", pk=unit.pk)
    return render(request, "org/form.html", {"form": form, "unit": unit})
