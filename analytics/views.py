from datetime import date, datetime, time

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils import timezone

from analytics import queries
from analytics.history import historical_dashboard
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
    end_month = forms.DateField(required=False, label="Ending month",
                                input_formats=["%Y-%m"],
                                widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        self.fields["org_unit"].label_from_instance = lambda u: u.indented_name
        self.fields["end_month"].widget.attrs["max"] = queries.last_complete_month().strftime("%Y-%m")

    def clean_end_month(self):
        value = self.cleaned_data["end_month"] or queries.last_complete_month()
        if value < date(1900, 1, 1) or value > queries.last_complete_month():
            raise forms.ValidationError("Choose a complete month between January 1900 and last month.")
        return value.replace(day=1)

    def clean_currency(self):
        value = (self.cleaned_data["currency"] or settings.DEFAULT_CURRENCY).upper()
        if not value.isascii() or not value.isalpha() or len(value) != 3:
            raise forms.ValidationError("Enter a three-letter currency code.")
        return value


@login_required
def analytics(request):
    params = request.GET.copy()
    for key, default in {"months": "12", "currency": settings.DEFAULT_CURRENCY,
                         "end_month": queries.last_complete_month().strftime("%Y-%m")}.items():
        if not params.get(key):
            params[key] = default
    form = AnalyticsFilter(params, user=request.user)
    if not form.is_valid():
        return render(request, "analytics/index.html", {
            "form": form, "invalid_filters": True,
        }, status=400 if request.GET.get("export") else 200)
    d = form.cleaned_data
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

    end = d["end_month"]
    start = queries.add_months(end, -(months - 1))
    until_date = queries.add_months(end, 1)
    since = timezone.make_aware(datetime.combine(start, time.min))
    until = timezone.make_aware(datetime.combine(until_date, time.min))
    tasks = scope(ApprovalTask.objects.filter(invoice__in=qs, decided_at__lt=until),
                  request.user, prefix="invoice__org_unit__")

    monthly = queries.monthly_spend(qs, months, end=end)
    by_unit = queries.spend_by_unit(qs, root, start, end)
    history = historical_dashboard(qs, months, end)
    for provider in history["providers"]:
        drill = params.copy()
        drill.pop("export", None)
        drill["provider"] = provider["id"]
        provider["url"] = "?" + drill.urlencode()

    if request.GET.get("export") == "csv":
        return csv_response("billing-history.csv",
                            ["Month", "Currency", "Invoices", "Providers", "Billed amount", "Recorded paid",
                             "Outstanding now", "Month-on-month change %"],
                            ([r["month"].isoformat(), currency, r["count"], r["provider_count"], r["total"],
                              r["paid"], r["outstanding"], r["change_pct"]] for r in history["ledger"]))
    if request.GET.get("export") == "providers":
        def safe_label(value):
            return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value

        return csv_response("provider-analysis.csv",
                            ["Provider", "Utility", "Currency", "Invoices", "Billed amount", "Share %",
                             "Recorded paid", "Outstanding now", "Previous period", "Change %"],
                            ([safe_label(p["name"]), p["utility"], currency, p["count"], p["total"], p["share"],
                              p["paid"], p["outstanding"], p["previous"], p["change_pct"]]
                             for p in history["providers"]))

    cohort = queries._billed(qs).filter(bill_month__gte=start, bill_month__lte=end)
    anomalies = cohort.filter(status__in=Invoice.OPEN).exclude(flags=[]).select_related("provider", "org_unit") \
        .order_by("due_date")[:25]
    return render(request, "analytics/index.html", {
        "form": form, "currency": currency, "months": months,
        "history": history,
        "monthly": monthly,
        "consumption": queries.consumption_trend(qs, months, end=end),
        "yoy": queries.year_over_year(qs, end=end),
        "by_unit": by_unit,
        "by_unit_chart": {"labels": [r["unit"].name for r in by_unit[:12]],
                          "series": [{"key": "total", "label": "Spend", "data": [float(r["total"]) for r in by_unit[:12]]}]},
        "providers": queries.top_providers(qs, start, end),
        "accounts": queries.top_accounts(qs, start, end),
        "sla": queries.sla_stats(tasks, since),
        "timeliness": queries.payment_timeliness(qs.filter(paid_at__lt=until_date), start),
        "cycle_days": queries.cycle_time(qs.filter(approved_at__lt=until), since),
        "forecast": queries.forecast_next_month(qs, end=end),
        "anomalies": [(a, [Flag.LABELS.get(f, f) for f in a.flags]) for a in anomalies],
    })
