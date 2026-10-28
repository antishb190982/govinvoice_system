from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils import timezone

from analytics import queries
from core.utils import csv_response
from django.shortcuts import render
from invoices.models import Flag, Invoice
from org.access import scope, units_for
from providers.models import Provider, UtilityType
from workflow.models import ApprovalTask


class AnalyticsFilter(forms.Form):
    org_unit = forms.ModelChoiceField(queryset=None, required=False, empty_label="All my units")
    utility = forms.ChoiceField(required=False, choices=[("", "All utilities")] + list(UtilityType.choices))
    provider = forms.ModelChoiceField(queryset=Provider.objects.all(), required=False, empty_label="All providers")
    months = forms.TypedChoiceField(coerce=int, initial=12, required=False,
                                    choices=[(6, "Last 6 months"), (12, "Last 12 months"), (24, "Last 24 months"),
                                             (36, "Last 36 months")])
    currency = forms.CharField(max_length=3, required=False, initial=settings.DEFAULT_CURRENCY)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        self.fields["org_unit"].label_from_instance = lambda u: u.indented_name


@login_required
def analytics(request):
    form = AnalyticsFilter(request.GET or None, user=request.user)
    d = form.cleaned_data if form.is_valid() else {}
    months = d.get("months") or 12
    currency = (d.get("currency") or settings.DEFAULT_CURRENCY).upper()
    root = d.get("org_unit")
    qs = scope(Invoice.objects.filter(currency=currency), request.user)
    if root:
        qs = qs.filter(org_unit__path__startswith=root.path)
    if d.get("utility"):
        qs = qs.filter(utility_type=d["utility"])
    if d.get("provider"):
        qs = qs.filter(provider=d["provider"])

    end = queries.last_complete_month()
    start = queries.add_months(end, -(months - 1))
    since = timezone.now() - timedelta(days=30 * months)
    tasks = scope(ApprovalTask.objects.all(), request.user, prefix="invoice__org_unit__")
    if root:
        tasks = tasks.filter(invoice__org_unit__path__startswith=root.path)

    monthly = queries.monthly_spend(qs, months)
    by_unit = queries.spend_by_unit(qs, root, start, end)

    if request.GET.get("export") == "csv":
        header = ["Month"] + [s["label"] for s in monthly["series"]] + ["Total"]
        rows = []
        for i, label in enumerate(monthly["labels"]):
            values = [s["data"][i] for s in monthly["series"]]
            rows.append([label] + values + [round(sum(values), 3)])
        return csv_response("spend-trend.csv", header, rows)

    anomalies = qs.filter(status__in=Invoice.OPEN).exclude(flags=[]).select_related("provider", "org_unit") \
        .order_by("due_date")[:25]
    return render(request, "analytics/index.html", {
        "form": form, "currency": currency, "months": months,
        "monthly": monthly,
        "consumption": queries.consumption_trend(qs, months),
        "yoy": queries.year_over_year(qs),
        "by_unit": by_unit,
        "by_unit_chart": {"labels": [r["unit"].name for r in by_unit[:12]],
                          "series": [{"key": "total", "label": "Spend", "data": [float(r["total"]) for r in by_unit[:12]]}]},
        "providers": queries.top_providers(qs, start, end),
        "accounts": queries.top_accounts(qs, start, end),
        "sla": queries.sla_stats(tasks, since),
        "timeliness": queries.payment_timeliness(qs, (timezone.now() - timedelta(days=30 * months)).date()),
        "cycle_days": queries.cycle_time(qs, since),
        "forecast": queries.forecast_next_month(qs),
        "anomalies": [(a, [Flag.LABELS.get(f, f) for f in a.flags]) for a in anomalies],
    })
