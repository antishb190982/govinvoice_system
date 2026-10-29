"""Historical billing cohorts, retaining Decimal amounts until chart rendering."""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, F, Sum
from django.db.models.functions import Coalesce, TruncMonth

from analytics import queries
from invoices.models import Invoice

ZERO = Decimal("0")


def change(current, previous):
    return round((current - previous) / previous * 100, 1) if previous else None


def historical_dashboard(qs, months, end):
    """The caller supplies an authorized, single-currency queryset."""
    start = queries.add_months(end, 1 - months)
    previous_start = queries.add_months(start, -months)
    labels = queries.month_range(start, end)
    billed = queries._billed(qs)
    rows = list(billed.filter(bill_month__gte=previous_start, bill_month__lte=end).values(
        "provider_id", "provider__name", "provider__short_code", "utility_type", "status",
        bm=F("bill_month"),
    ).annotate(total=Sum("amount_total"), count=Count("pk")).order_by())
    providers = {}
    monthly = {month: {"month": month, "total": ZERO, "count": 0, "paid": ZERO,
                       "outstanding": ZERO, "providers": set()} for month in labels}
    provider_months = defaultdict(dict)
    previous_total, previous_count = ZERO, 0
    for row in rows:
        pk, month = row["provider_id"], row["bm"]
        amount = row["total"].quantize(Decimal("0.001"))
        if pk not in providers:
            providers[pk] = {
                "id": pk, "name": row["provider__name"], "code": row["provider__short_code"],
                "utility": queries.UTILITY_LABELS.get(row["utility_type"], row["utility_type"]),
                "total": ZERO, "previous": ZERO, "paid": ZERO, "outstanding": ZERO, "count": 0,
                "previous_count": 0,
            }
        provider = providers[pk]
        if month < start:
            provider["previous"] += amount
            provider["previous_count"] += row["count"]
            previous_total += amount
            previous_count += row["count"]
            continue
        provider["total"] += amount
        provider["count"] += row["count"]
        item = monthly[month]
        item["total"] += amount
        item["count"] += row["count"]
        item["providers"].add(pk)
        point = provider_months[pk].setdefault(month, {"total": ZERO, "count": 0})
        point["total"] += amount
        point["count"] += row["count"]
        if row["status"] == Invoice.Status.PAID:
            provider["paid"] += amount
            item["paid"] += amount
        if row["status"] in Invoice.UNPAID:
            provider["outstanding"] += amount
            item["outstanding"] += amount

    totals = {key: sum((m[key] for m in monthly.values()), ZERO)
              for key in ["total", "paid", "outstanding"]}
    count = sum(m["count"] for m in monthly.values())
    totals.update(count=count, previous=previous_total, previous_count=previous_count,
                  change_pct=change(totals["total"], previous_total) if count and previous_count else None)
    totals["average"] = totals["total"] / totals["count"] if totals["count"] else None
    provider_rows = sorted(providers.values(), key=lambda p: (-p["total"], p["name"]))
    for provider in provider_rows:
        provider["share"] = round(provider["total"] / totals["total"] * 100, 1) if totals["total"] else None
        provider["average"] = provider["total"] / provider["count"] if provider["count"] else None
        provider["change_pct"] = change(provider["total"], provider["previous"]) \
            if provider["count"] and provider["previous_count"] else None

    ledger, last = [], None
    for item in monthly.values():
        item["provider_count"] = len(item.pop("providers"))
        # A missing month's data is not evidence of an actual reduction.
        item["change_pct"] = change(item["total"], last["total"]) if last and item["count"] and last["count"] else None
        ledger.append(item)
        last = item

    active = [p for p in provider_rows if p["count"]]
    totals["provider_count"] = len(active)
    chart_providers = active[:6]
    remainder = active[6:]
    series = [
        {"key": str(p["id"]), "label": p["name"],
         "data": [float(provider_months[p["id"]].get(m, {}).get("total", ZERO)) for m in labels]}
        for p in chart_providers
    ]
    if remainder:
        series.append({"key": "other", "label": "Other providers", "data": [
            float(sum((provider_months[p["id"]].get(m, {}).get("total", ZERO) for p in remainder), ZERO))
            for m in labels
        ]})
    ranking = active[:8]
    ranking_labels = [p["name"] for p in ranking]
    ranking_values = [float(p["total"]) for p in ranking]
    if len(active) > 8:
        ranking_labels.append("Other providers")
        ranking_values.append(float(sum((p["total"] for p in active[8:]), ZERO)))

    insights = []
    prior_months = [queries.add_months(end, -i) for i in (1, 2, 3)]
    for p in provider_rows:
        points = provider_months[p["id"]]
        if not all(points.get(m, {}).get("count") for m in prior_months):
            continue
        baseline = sum((points[m]["total"] for m in prior_months), ZERO) / 3
        if not baseline:
            continue
        latest = points.get(end, {"total": ZERO, "count": 0})
        pct = change(latest["total"], baseline)
        if not latest["count"]:
            insights.append({"provider": p, "kind": "gap", "title": "Missing latest-month billing",
                             "baseline": baseline, "latest": None, "change_pct": None,
                             "count": 0, "baseline_count": sum(points[m]["count"] for m in prior_months) / 3})
        elif abs(pct) >= 20:
            insights.append({"provider": p, "kind": "rise" if pct > 0 else "fall",
                             "title": "Billing increase" if pct > 0 else "Billing decrease",
                             "baseline": baseline, "latest": latest["total"], "change_pct": pct,
                             "count": latest["count"],
                             "baseline_count": sum(points[m]["count"] for m in prior_months) / 3})
    insights.sort(key=lambda i: (-abs(i["change_pct"] or 0), i["provider"]["name"]))
    drafts = qs.filter(status=Invoice.Status.DRAFT).alias(
        bm=TruncMonth(Coalesce("period_end", "issue_date")),
    ).filter(bm__gte=start, bm__lte=end).count()
    return {
        "start": start, "end": end, "period_end": queries.add_months(end, 1),
        "previous_start": previous_start, "previous_end": queries.add_months(start, -1),
        "totals": totals, "providers": provider_rows, "ledger": ledger, "insights": insights,
        "drafts_excluded": drafts,
        "provider_trend": {"labels": [m.strftime("%b %Y") for m in labels], "series": series},
        "provider_chart": {"labels": ranking_labels,
                           "series": [{"key": "billed", "label": "Billed amount", "data": ranking_values}]},
    }
