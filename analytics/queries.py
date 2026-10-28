"""Aggregations behind the dashboard and trend analysis. All take an already-scoped queryset."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, DecimalField, F, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from invoices.models import Invoice
from org.models import OrgUnit
from providers.models import CONSUMPTION_UNITS, UtilityType

UTILITY_LABELS = dict(UtilityType.choices)
BILLABLE = [s for s in Invoice.Status.values if s not in (Invoice.Status.CANCELLED, Invoice.Status.DRAFT)]


def month_floor(d):
    return d.replace(day=1)


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def month_range(start, end):
    months, cur = [], month_floor(start)
    while cur <= end:
        months.append(cur)
        cur = add_months(cur, 1)
    return months


def _billed(qs):
    """Attribute each invoice to the month its consumption ended (or was issued).
    alias() (not annotate) keeps the month out of GROUP BY unless a query asks for it."""
    return qs.filter(status__in=BILLABLE).alias(bill_month=TruncMonth(Coalesce("period_end", "issue_date")))


def last_complete_month():
    return add_months(month_floor(timezone.localdate()), -1)


def _series(rows, months, key, value):
    data = defaultdict(lambda: {m: 0.0 for m in months})
    for r in rows:
        m = r["bm"]
        m = m.date() if hasattr(m, "date") and not isinstance(m, date) else m
        if m in months:
            data[r[key]][m] += float(r[value] or 0)
    return data


def monthly_spend(qs, months=12, end=None):
    end = month_floor(end) if end else last_complete_month()
    start = add_months(end, -(months - 1))
    labels = month_range(start, end)
    rows = _billed(qs).filter(bill_month__gte=start, bill_month__lte=end) \
        .values("utility_type", bm=F("bill_month")).annotate(total=Sum("amount_total"))
    series = _series(rows, labels, "utility_type", "total")
    return {
        "labels": [m.strftime("%b %Y") for m in labels],
        "series": [{"key": k, "label": UTILITY_LABELS.get(k, k), "data": [round(v[m], 3) for m in labels]}
                   for k, v in sorted(series.items())],
    }


def consumption_trend(qs, months=12, end=None):
    end = month_floor(end) if end else last_complete_month()
    start = add_months(end, -(months - 1))
    labels = month_range(start, end)
    rows = _billed(qs).filter(bill_month__gte=start, bill_month__lte=end, consumption__isnull=False) \
        .values("utility_type", bm=F("bill_month")).annotate(total=Sum("consumption"))
    series = _series(rows, labels, "utility_type", "total")
    out = []
    for k, v in sorted(series.items()):
        values = [v[m] for m in labels]
        present = [x for x in values if x]
        avg = sum(present) / len(present) if present else 0
        # Units differ (kWh, m³, GB…), so each utility is indexed to its own average = 100.
        out.append({"key": k, "label": f"{UTILITY_LABELS.get(k, k)} ({CONSUMPTION_UNITS.get(k, '')})",
                    "data": [round(x / avg * 100, 1) if avg and x else None for x in values],
                    "average": round(avg, 1)})
    return {"labels": [m.strftime("%b %Y") for m in labels], "series": out}


def _totals_by_utility(qs, start, end):
    out = defaultdict(Decimal)
    for r in _billed(qs).filter(bill_month__gte=start, bill_month__lte=end).values("utility_type", "amount_total"):
        out[r["utility_type"]] += r["amount_total"]
    return out


def year_over_year(qs, end=None):
    end = month_floor(end) if end else last_complete_month()
    cur_start, prev_start = add_months(end, -11), add_months(end, -23)
    cur = _totals_by_utility(qs, cur_start, end)
    prev = _totals_by_utility(qs, prev_start, add_months(cur_start, -1))
    out = []
    for key in sorted(set(cur) | set(prev)):
        c, p = cur.get(key) or Decimal(0), prev.get(key) or Decimal(0)
        out.append({"utility": UTILITY_LABELS.get(key, key), "current": c, "previous": p,
                    "change_pct": round(float((c - p) / p * 100), 1) if p else None})
    return out


def spend_by_unit(qs, root=None, start=None, end=None):
    """Totals per child of `root` (or per top-level unit)."""
    billed = _billed(qs)
    if start:
        billed = billed.filter(bill_month__gte=start)
    if end:
        billed = billed.filter(bill_month__lte=end)
    if root is None:
        tops = list(OrgUnit.objects.filter(parent__isnull=True))
        root = tops[0] if len(tops) == 1 else None
    children = list(root.children.all()) if root else list(OrgUnit.objects.filter(parent__isnull=True))
    if root and not children:
        children = [root]
    out = []
    for child in children:
        agg = billed.filter(org_unit__path__startswith=child.path).aggregate(t=Sum("amount_total"), n=Count("id"))
        if agg["n"]:
            out.append({"unit": child, "total": agg["t"], "count": agg["n"]})
    direct = billed.filter(org_unit=root).aggregate(t=Sum("amount_total"), n=Count("id")) if root and root not in children else None
    if direct and direct["n"]:
        out.append({"unit": root, "total": direct["t"], "count": direct["n"], "direct": True})
    return sorted(out, key=lambda r: r["total"], reverse=True)


def top_providers(qs, start=None, end=None, limit=10):
    billed = _billed(qs)
    if start:
        billed = billed.filter(bill_month__gte=start)
    if end:
        billed = billed.filter(bill_month__lte=end)
    return list(billed.values("provider__name", "utility_type").annotate(total=Sum("amount_total"), n=Count("id"))
                .order_by("-total")[:limit])


def top_accounts(qs, start=None, end=None, limit=10):
    billed = _billed(qs)
    if start:
        billed = billed.filter(bill_month__gte=start)
    if end:
        billed = billed.filter(bill_month__lte=end)
    return list(billed.values("service_account_id", "service_account__account_number", "service_account__premises",
                              "provider__short_code", "org_unit__code")
                .annotate(total=Sum("amount_total"), consumption=Sum("consumption"), n=Count("id"))
                .order_by("-total")[:limit])


def due_profile(unpaid_qs, weeks=6):
    """Unpaid amounts by due week, split by status, starting with everything overdue."""
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    labels = ["Overdue"] + [f"Wk of {(week_start + timedelta(weeks=i)):%d %b}" for i in range(weeks)]
    groups = {"in_approval": "In approval", "approved": "Approved", "scheduled": "In instruction", "other": "Other"}
    data = {k: [0.0] * len(labels) for k in groups}
    for inv in unpaid_qs.values("due_date", "status", "amount_total"):
        if inv["due_date"] < today:
            idx = 0
        else:
            idx = 1 + (inv["due_date"] - week_start).days // 7
            if idx >= len(labels):
                continue
        key = inv["status"] if inv["status"] in groups else "other"
        data[key][idx] += float(inv["amount_total"])
    return {"labels": labels, "series": [{"key": k, "label": v, "data": data[k]} for k, v in groups.items()
                                         if any(data[k])]}


def forecast_next_month(qs):
    """Naive projection per utility: mean of the last 3 complete months, adjusted by
    last year's month-on-month seasonality when a year of history exists."""
    end = add_months(month_floor(timezone.localdate()), -1)
    labels = month_range(add_months(end, -13), end)
    rows = _billed(qs).filter(bill_month__gte=labels[0], bill_month__lte=end) \
        .values("utility_type", bm=F("bill_month")).annotate(total=Sum("amount_total"))
    series = _series(rows, labels, "utility_type", "total")
    target = add_months(end, 1)
    out = []
    for key, values in sorted(series.items()):
        recent = [values[m] for m in labels[-3:]]
        base = sum(recent) / 3 if any(recent) else 0
        ly_prev, ly_target = values.get(add_months(target, -13), 0), values.get(add_months(target, -12), 0)
        factor = (ly_target / ly_prev) if ly_prev and ly_target else 1.0
        factor = min(max(factor, 0.5), 2.0)
        out.append({"utility": UTILITY_LABELS.get(key, key), "projection": round(base * factor, 3),
                    "basis": round(base, 3), "seasonal_factor": round(factor, 2)})
    return {"month": target, "rows": out}


def sla_stats(tasks_qs, since):
    decided = tasks_qs.filter(decided_at__gte=since, decided_at__isnull=False)
    by_step = list(decided.values("name").annotate(
        n=Count("id"), breached=Count("id", filter=Q(breached=True))).order_by("name"))
    for row in by_step:
        row["on_time_pct"] = round(100 * (row["n"] - row["breached"]) / row["n"]) if row["n"] else None
    total = decided.count()
    breached = decided.filter(breached=True).count()
    from core.worktime import working_hours_between

    durations = [working_hours_between(t.activated_at, t.decided_at)
                 for t in decided.exclude(activated_at__isnull=True).only("activated_at", "decided_at")[:2000]]
    return {"decided": total, "breached": breached,
            "on_time_pct": round(100 * (total - breached) / total) if total else None,
            "avg_hours": round(sum(durations) / len(durations), 1) if durations else None,
            "by_step": by_step}


def payment_timeliness(qs, since):
    paid = qs.filter(status=Invoice.Status.PAID, paid_at__gte=since, paid_at__isnull=False)
    total = paid.count()
    on_time = paid.filter(paid_at__lte=F("due_date")).count()
    late_amount = paid.filter(paid_at__gt=F("due_date")).aggregate(
        t=Coalesce(Sum("amount_total"), Decimal(0), output_field=DecimalField()))["t"]
    return {"paid": total, "on_time": on_time, "on_time_pct": round(100 * on_time / total) if total else None,
            "late_amount": late_amount}


def cycle_time(qs, since):
    rows = qs.filter(approved_at__gte=since, submitted_at__isnull=False).values_list("created_at", "approved_at")
    days = [(a - c).total_seconds() / 86400 for c, a in rows]
    return round(sum(days) / len(days), 1) if days else None


def account_history(account, months=24):
    end = last_complete_month()
    labels = month_range(add_months(end, -(months - 1)), end)
    rows = _billed(Invoice.objects.filter(service_account=account)).values(bm=F("bill_month")) \
        .annotate(total=Sum("amount_total"), consumption=Sum("consumption"))
    by_month = {}
    for r in rows:
        m = r["bm"]
        by_month[m.date() if hasattr(m, "date") and not isinstance(m, date) else m] = r
    return {
        "labels": [m.strftime("%b %y") for m in labels],
        "series": [
            {"key": "amount", "label": "Amount", "data": [float(by_month.get(m, {}).get("total") or 0) for m in labels]},
            {"key": "consumption", "label": f"Consumption ({account.provider.consumption_unit})",
             "data": [float(by_month.get(m, {}).get("consumption") or 0) for m in labels]},
        ],
        "average": Invoice.objects.filter(service_account=account, status__in=BILLABLE).aggregate(a=Avg("amount_total"))["a"],
    }
