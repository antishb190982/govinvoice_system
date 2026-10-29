"""Observed ministry receipt/submission timing, not an attribution of fault."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from core.provider_scope import available_providers
from core.utils import csv_response, paginate
from invoices.models import Invoice
from org.access import scope, units_for
from org.models import OrgUnit


class TimelinessFilter(forms.Form):
    start = forms.DateField(label="Issue date from", widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(label="Issue date to", widget=forms.DateInput(attrs={"type": "date"}))
    org_unit = forms.ModelChoiceField(queryset=OrgUnit.objects.none(), required=False, label="Ministry / unit", empty_label="All authorised units")
    provider = forms.ModelChoiceField(queryset=None, required=False, label="Utility provider", empty_label="All providers")
    currency = forms.ChoiceField()
    threshold = forms.IntegerField(min_value=2, max_value=1000, label="Repeat threshold", initial=2)
    repeated = forms.BooleanField(required=False, label="Repeat delays only")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        self.fields["provider"].queryset = available_providers(user)
        currencies = set(scope(Invoice.objects.all(), user).values_list("currency", flat=True))
        currencies.add(settings.DEFAULT_CURRENCY)
        self.fields["currency"].choices = [(c, c) for c in sorted(currencies) if c]

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end"):
            if data["start"] > data["end"]:
                raise forms.ValidationError("The start date must be before the end date.")
            if (data["end"] - data["start"]).days > 3660:
                raise forms.ValidationError("Choose a reporting period of ten years or less.")
        return data


def build_report(queryset, threshold):
    units = {u.pk: u for u in OrgUnit.objects.only("id", "name", "kind")}
    groups, records = {}, []
    monthly = defaultdict(lambda: {"invoices": 0, "late": 0, "surcharge": Decimal("0")})
    for invoice in queryset.select_related("org_unit", "provider").iterator(chunk_size=500):
        chain = [units[pk] for pk in invoice.org_unit.ancestor_ids if pk in units]
        ministry = next((u for u in reversed(chain) if u.kind in {"ministry", "authority"}), chain[0] if chain else invoice.org_unit)
        row = groups.setdefault(ministry.pk, {
            "id": ministry.pk, "name": ministry.name, "invoices": 0, "known_receipts": 0,
            "missing_receipts": 0, "invalid_receipts": 0, "late_receipts": 0, "submitted": 0, "late_submissions": 0,
            "recording_delays": 0, "late": 0, "surcharge": Decimal("0"), "late_surcharge": Decimal("0"),
        })
        submitted = timezone.localtime(invoice.submitted_at).date() if invoice.submitted_at else None
        recorded = timezone.localtime(invoice.created_at).date()
        receipt_valid = bool(invoice.received_date and invoice.issue_date <= invoice.received_date <= timezone.localdate())
        receipt_anomaly = bool(invoice.received_date and not receipt_valid)
        received_late = bool(receipt_valid and invoice.received_date > invoice.due_date)
        submitted_late = bool(submitted and submitted > invoice.due_date)
        recording_delay = bool(receipt_valid and (recorded - invoice.received_date).days > 7)
        late = received_late or submitted_late
        row["invoices"] += 1
        row["known_receipts"] += receipt_valid
        row["missing_receipts"] += not bool(invoice.received_date)
        row["invalid_receipts"] += receipt_anomaly
        row["submitted"] += bool(submitted)
        row["late_receipts"] += received_late
        row["late_submissions"] += submitted_late
        row["recording_delays"] += recording_delay
        row["late"] += late
        row["surcharge"] += invoice.surcharge_amount
        row["late_surcharge"] += invoice.surcharge_amount if late else Decimal("0")
        month = monthly[invoice.issue_date.strftime("%Y-%m")]
        month["invoices"] += 1
        month["late"] += late
        month["surcharge"] += invoice.surcharge_amount
        records.append({"invoice": invoice, "ministry": ministry.name, "ministry_id": ministry.pk,
                        "received_late": received_late, "submitted_late": submitted_late,
                        "recording_delay": recording_delay, "receipt_anomaly": receipt_anomaly,
                        "late": late, "submitted_date": submitted})
    for row in groups.values():
        row["repeat"] = row["late"] >= threshold
        row["late_rate"] = round(100 * row["late"] / row["invoices"], 1)
        row["receipt_rate"] = round(100 * row["late_receipts"] / row["known_receipts"], 1) if row["known_receipts"] else None
    return sorted(groups.values(), key=lambda r: (-r["late"], -r["late_surcharge"], r["name"])), records, monthly


@login_required
def ministry_timeliness(request):
    today = timezone.localdate()
    params = request.GET.copy()
    for key, default in {"start": today - timedelta(days=365), "end": today,
                         "currency": settings.DEFAULT_CURRENCY, "threshold": 2}.items():
        if not params.get(key):
            params[key] = str(default)
    if not params.get("provider") and getattr(request, "utility_provider", None):
        params["provider"] = str(request.utility_provider.pk)
    form = TimelinessFilter(params, user=request.user)
    context = {"form": form, "tab": "invoices" if params.get("tab") == "invoices" else "overview"}
    if not form.is_valid():
        return render(request, "analytics/ministry_timeliness.html", context, status=400)
    data = form.cleaned_data
    invoices = scope(Invoice.objects.exclude(status=Invoice.Status.CANCELLED), request.user).filter(
        currency=data["currency"], issue_date__range=(data["start"], data["end"]))
    if data["org_unit"]:
        invoices = invoices.filter(org_unit__path__startswith=data["org_unit"].path)
    if data["provider"]:
        invoices = invoices.filter(provider=data["provider"])
    rows, records, monthly = build_report(invoices, data["threshold"])
    if data["repeated"]:
        rows = [row for row in rows if row["repeat"]]
        ids = {row["id"] for row in rows}
        records = [r for r in records if r["ministry_id"] in ids]
        # Monthly totals must use the same repeat-only cohort as the dashboard.
        monthly = defaultdict(lambda: {"invoices": 0, "late": 0, "surcharge": Decimal("0")})
        for record in records:
            month = monthly[record["invoice"].issue_date.strftime("%Y-%m")]
            month["invoices"] += 1
            month["late"] += record["late"]
            month["surcharge"] += record["invoice"].surcharge_amount
    query = params.copy()
    query.pop("export", None)
    query.pop("page", None)
    def url(**changes):
        result = query.copy()
        for key, value in changes.items():
            result[key] = str(value)
        return "?" + result.urlencode()
    for row in rows:
        # Evidence is filtered by the roll-up ministry after the original access scope.
        row["url"] = url(tab="invoices", evidence_ministry=row["id"])
    evidence_id = params.get("evidence_ministry")
    if evidence_id:
        records = [r for r in records if str(r["ministry_id"]) == evidence_id]
        rows = [r for r in rows if str(r["id"]) == evidence_id]
        monthly = defaultdict(lambda: {"invoices": 0, "late": 0, "surcharge": Decimal("0")})
        for record in records:
            month = monthly[record["invoice"].issue_date.strftime("%Y-%m")]
            month["invoices"] += 1
            month["late"] += record["late"]
            month["surcharge"] += record["invoice"].surcharge_amount
    if params.get("export") == "summary":
        return csv_response("govbills-ministry-timeliness.csv",
            ["Ministry (authorised records only)", "Currency", "Invoices", "Valid receipt dates", "Missing receipt dates", "Invalid receipt dates",
             "Late receipts", "Submitted", "Late submissions", "Recording delays over 7 days", "Late invoices",
             "Late invoice rate %", "Repeat delays", "Billed surcharge", "Surcharge on late invoices"],
            ([r["name"], data["currency"], r["invoices"], r["known_receipts"], r["missing_receipts"], r["invalid_receipts"],
              r["late_receipts"], r["submitted"], r["late_submissions"], r["recording_delays"], r["late"],
              r["late_rate"], r["repeat"], r["surcharge"], r["late_surcharge"]] for r in rows))
    if params.get("export") == "invoices":
        return csv_response("govbills-ministry-timing-evidence.csv",
            ["Ministry", "Unit", "Provider", "Invoice", "Issue date", "Receipt date", "Due date", "Submitted date",
             "Late receipt", "Late submission", "Recorded over 7 days after receipt", "Receipt date anomaly", "Currency", "Billed surcharge"],
            ([r["ministry"], r["invoice"].org_unit.name, r["invoice"].provider.name, r["invoice"].reference,
              r["invoice"].issue_date, r["invoice"].received_date, r["invoice"].due_date, r["submitted_date"],
              r["received_late"], r["submitted_late"], r["recording_delay"], r["receipt_anomaly"], r["invoice"].currency,
              r["invoice"].surcharge_amount] for r in records))
    totals = {key: sum((row[key] for row in rows), 0) for key in (
        "invoices", "late", "late_receipts", "late_submissions", "missing_receipts", "invalid_receipts", "surcharge", "late_surcharge")}
    totals["repeat"] = sum(row["repeat"] for row in rows)
    context.update(rows=rows, totals=totals, currency=data["currency"], data=data,
        page=paginate(request, records, 25), monthly=[dict(month=key, **value) for key, value in sorted(monthly.items())],
        overview_url=url(tab="overview", evidence_ministry=""), evidence_url=url(tab="invoices", evidence_ministry=""),
        summary_csv=url(export="summary"), evidence_csv=url(export="invoices"))
    return render(request, "analytics/ministry_timeliness.html", context)
