import calendar
from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import AuditEvent
from core.utils import paginate
from core.provider_scope import available_providers
from imports.models import ImportBatch, ImportRow
from invoices.models import Invoice
from org.access import scope
from providers.models import BillingCycle, Provider
from providers.views import is_master_data_admin
from reconciliation.models import Statement


class BillingCycleForm(forms.ModelForm):
    class Meta:
        model = BillingCycle
        fields = ["provider", "code", "name", "period_start", "period_end"]
        widgets = {key: forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
                   for key in ("period_start", "period_end")}
        help_texts = {
            "code": "A unique cycle code within this provider, e.g. NOV-2026-A.",
            "name": "Separate overlapping cycles with meaningful names, e.g. November — cycle A.",
            "period_end": "Overlapping dates are allowed. These dates do not merge separate cycles.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["period_start"].required = self.fields["period_end"].required = True

    def clean_code(self):
        value = self.cleaned_data["code"].strip().upper()
        if value == "UNASSIGNED":
            raise forms.ValidationError("This code is reserved for the awaiting-assignment queue.")
        return value

    def clean(self):
        data = super().clean()
        if data.get("period_start") and data.get("period_end") and data["period_start"] > data["period_end"]:
            self.add_error("period_end", "Cycle end cannot precede cycle start.")
        return data


def _uploads(user):
    qs = ImportBatch.objects.all()
    return qs if user.is_superuser else qs.filter(uploaded_by=user)


def entries_for(cycles, user):
    cycles = list(cycles)
    ids = [c.pk for c in cycles]
    pending = ~Q(status__in=[Invoice.Status.PAID, Invoice.Status.CANCELLED])
    counts = {
        row["billing_cycle_id"]: row
        for row in scope(Invoice.objects.filter(billing_cycle_id__in=ids), user)
        .values("billing_cycle_id").annotate(
            received=Count("pk"), processed=Count("pk", filter=Q(status=Invoice.Status.PAID)),
            pending=Count("pk", filter=pending), cancelled=Count("pk", filter=Q(status=Invoice.Status.CANCELLED)),
            overdue=Count("pk", filter=pending & Q(due_date__lt=timezone.localdate())),
        )
    }
    statement_counts = {
        r["billing_cycle_id"]: r["n"]
        for r in scope(Statement.objects.filter(billing_cycle_id__in=ids), user)
        .values("billing_cycle_id").annotate(n=Count("pk"))
    }
    # Count individual document sources, but a spreadsheet only once per cycle.
    sources = {}
    rows = ImportRow.objects.filter(
        billing_cycle_id__in=ids, batch__in=_uploads(user),
    ).values_list("billing_cycle_id", "batch_id", "source_file", "source_name")
    for cycle_id, batch_id, source_file, source_name in rows:
        sources.setdefault(cycle_id, set()).add((batch_id, source_name if source_file else "spreadsheet"))
    result = []
    for cycle in cycles:
        row = counts.get(cycle.pk, {})
        url = cycle.get_absolute_url()
        result.append({
            "cycle": cycle, **{k: row.get(k, 0) for k in (
                "received", "processed", "pending", "cancelled", "overdue")},
            "files_count": statement_counts.get(cycle.pk, 0) + len(sources.get(cycle.pk, set())),
            "url": url, "processed_url": url + "?status=processed", "pending_url": url + "?status=pending",
        })
    return result


@login_required
def calendar_view(request):
    providers = available_providers(request.user)
    selected = request.GET.get("provider", "")
    if not selected and getattr(request, "utility_provider", None):
        selected = str(request.utility_provider.pk)
    if selected and not providers.filter(pk=selected if selected.isdigit() else -1).exists():
        raise Http404
    raw = request.GET.get("month", "")
    try:
        month = date.fromisoformat(raw + "-01") if raw else timezone.localdate().replace(day=1)
        if month.year < 2 or month.year > 9998:
            raise ValueError
    except ValueError:
        month = timezone.localdate().replace(day=1)
        messages.warning(request, "Invalid month; showing the current month.")
    end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    cycles = BillingCycle.objects.filter(provider__in=providers).select_related("provider")
    if selected:
        cycles = cycles.filter(provider_id=selected)
    entries = entries_for(cycles.filter(period_end__range=(month, end)), request.user)
    by_date = {}
    for entry in entries:
        by_date.setdefault(entry["cycle"].period_end, []).append(entry)
    weeks = [[{"date": day, "day": day.day, "in_month": day.month == month.month,
               "entries": by_date.get(day, [])}
              for day in week]
             for week in calendar.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)]
    unassigned = entries_for(cycles.filter(code="UNASSIGNED"), request.user)
    unassigned = [e for e in unassigned if e["received"] or e["files_count"]]
    return render(request, "providers/cycles.html", {
        "hide_provider_tabs": True,
        "providers": providers, "selected_provider": selected, "month": month, "weeks": weeks,
        "previous_month": (month - timedelta(days=1)).strftime("%Y-%m"),
        "next_month": (end + timedelta(days=1)).strftime("%Y-%m"),
        "totals": {key: sum(e[key] for e in entries) for key in ("received", "processed", "pending", "overdue")},
        "unassigned": unassigned, "can_manage": is_master_data_admin(request.user),
    })


@login_required
def detail(request, pk):
    cycle = get_object_or_404(BillingCycle.objects.select_related("provider"),
                             pk=pk, provider__in=available_providers(request.user))
    entry = entries_for([cycle], request.user)[0]
    invoices = scope(Invoice.objects.filter(billing_cycle=cycle).select_related(
        "service_account", "org_unit", "provider"), request.user)
    status = request.GET.get("status", "all")
    if status == "processed":
        invoices = invoices.filter(status=Invoice.Status.PAID)
    elif status == "pending":
        invoices = invoices.exclude(status__in=[Invoice.Status.PAID, Invoice.Status.CANCELLED])
    elif status == "cancelled":
        invoices = invoices.filter(status=Invoice.Status.CANCELLED)
    else:
        status = "all"
    page = paginate(request, invoices.order_by("due_date", "pk"), 50)
    return render(request, "providers/cycle_detail.html", {
        "hide_provider_tabs": True,
        "cycle": cycle, "entry": entry, "invoices": page, "page": page,
        "statements": scope(Statement.objects.filter(billing_cycle=cycle), request.user),
        "uploads": _uploads(request.user).filter(Q(billing_cycles=cycle) | Q(billing_cycle=cycle)).distinct(),
        "status": status, "can_manage": is_master_data_admin(request.user),
    })


@login_required
@transaction.atomic
def create(request):
    if not is_master_data_admin(request.user):
        raise PermissionDenied
    form = BillingCycleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cycle = form.save()
        AuditEvent.record("provider.billing_cycle_created", obj=cycle, actor=request.user, data={
            "provider": cycle.provider_id, "code": cycle.code,
            "period_start": cycle.period_start.isoformat(), "period_end": cycle.period_end.isoformat(),
        })
        messages.success(request, "Billing cycle created. Overlapping cycles retain separate identities.")
        return redirect(cycle)
    return render(request, "providers/cycle_form.html", {"form": form, "hide_provider_tabs": True})
