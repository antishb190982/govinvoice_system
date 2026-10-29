from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import render
from django.utils import timezone
from invoices.models import Invoice
from operations.models import InvoiceBatch, DisputeCase
from org.access import scope
from providers.models import ServiceAccount
from settlements.models import SettlementInstruction
from core.provider_scope import available_providers
from analytics import queries


@login_required
def dashboard(request):
    invoices = scope(Invoice.objects.all(), request.user)
    accounts = scope(ServiceAccount.objects.all(), request.user)
    batches = scope(InvoiceBatch.objects.all(), request.user)
    settlements = scope(SettlementInstruction.objects.all(), request.user)
    open_invoices = invoices.filter(status__in=Invoice.UNPAID)
    end = queries.last_complete_month()
    months = queries.month_range(queries.add_months(end, -11), end)
    trend_rows = invoices.alias(provider_month=TruncMonth(Coalesce("period_end", "issue_date"))) \
        .filter(status__in=queries.BILLABLE, provider_month__gte=months[0], provider_month__lte=end) \
        .values("currency", month=F("provider_month")).annotate(total=Sum("amount_total"))
    month_data = {}
    for row in trend_rows:
        month = row["month"]
        month = month.date() if hasattr(month, "date") and not hasattr(month, "year_day") else month
        month_data.setdefault(row["currency"], {label: 0.0 for label in months})
        if month in month_data[row["currency"]]:
            month_data[row["currency"]][month] += float(row["total"] or 0)
    trends = [{"id": f"provider-trend-{index}", "currency": currency,
        "data": {"labels": [month.strftime("%b %Y") for month in months],
                 "series": [{"key": currency, "label": currency,
                             "data": [round(month_data[currency][month], 3) for month in months]}]}}
        for index, currency in enumerate(sorted(month_data))]
    return render(request, "providers/visibility.html", {
        "invoice_count": invoices.count(), "account_count": accounts.count(),
        "unbatched_count": invoices.exclude(batch_memberships__active=True).exclude(
            status__in=["paid", "cancelled", "scheduled", "closed_zero"]).count(),
        "overdue_count": open_invoices.filter(due_date__lt=timezone.localdate()).count(),
        "batch_count": batches.filter(cancelled_at__isnull=True).count(),
        "dispute_count": scope(DisputeCase.objects.filter(status__in=["open", "authorisation", "refund_due"]),
                               request.user).count(),
        "amounts": open_invoices.values("currency").annotate(total=Sum("amount_total")).order_by("currency"),
        "statuses": invoices.values("status").annotate(count=Count("pk")).order_by("status"),
        "provider_rows": invoices.values("provider_id", "provider__name", "currency").annotate(
            count=Count("pk"), total=Sum("amount_total")).order_by("provider__name", "currency"),
        "recent_batches": batches.select_related("provider", "ministry")[:12],
        "settlement_count": settlements.exclude(status="cancelled").count(),
        "trends": trends,
    })
