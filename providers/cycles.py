"""Cycle classification only. Never changes an invoice's signed snapshot."""
from datetime import date

from django.core.exceptions import ValidationError

from providers.models import BillingCycle


def unassigned_cycle(provider):
    return BillingCycle.objects.get_or_create(
        provider=provider, code="UNASSIGNED",
        defaults={"name": "Awaiting cycle assignment"},
    )[0]


def classify(provider, period_start=None, period_end=None, selected=None):
    """Use explicit identity, or one unambiguous period. Never guess between cycles."""
    for field, value in (("start", period_start), ("end", period_end)):
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError as exc:
                raise ValidationError(f"Invalid billing period {field}.") from exc
            if field == "start":
                period_start = value
            else:
                period_end = value
    if selected and not selected.is_unassigned:
        if selected.provider_id != provider.pk:
            raise ValidationError("The billing cycle belongs to a different utility provider.")
        if (period_start and not selected.period_start <= period_start <= selected.period_end) or (
                period_end and not selected.period_start <= period_end <= selected.period_end):
            raise ValidationError("The invoice period is outside the selected billing cycle.")
        return selected
    if period_start and period_end and period_start <= period_end:
        candidates = list(BillingCycle.objects.filter(
            provider=provider, period_start__lte=period_start,
            period_end__gte=period_end).order_by("pk")[:2])
        if len(candidates) == 1:
            return candidates[0]
    return unassigned_cycle(provider)


def classify_invoice(invoice):
    selected = invoice.billing_cycle if invoice.billing_cycle_id else None
    cycle = classify(invoice.provider, invoice.period_start, invoice.period_end, selected)
    if not selected or selected.is_unassigned:
        # A loaded file can identify a named cycle even where two periods overlap.
        # Only current, scoped, non-ledger source rows are evidence for this.
        from reconciliation.models import StatementRow
        from reconciliation.parser import norm
        from reconciliation.services import latest_statement
        rows = StatementRow.objects.filter(
            statement__provider=invoice.provider,
            statement__currency=invoice.currency,
            statement__is_active=True,
            statement__org_unit_id__in=invoice.org_unit.ancestor_ids,
            statement__statement_date__gte=invoice.issue_date,
            statement__billing_cycle__isnull=False,
            assigned_unit_id__in=invoice.org_unit.ancestor_ids,
            invoice_number__iexact=invoice.invoice_number,
            amount=invoice.amount_total, parse_error="", is_ledger_only=False,
        ).select_related("statement__billing_cycle", "assigned_unit")
        evidence = {}
        for row in rows:
            candidate = row.statement.billing_cycle
            if candidate.is_unassigned or norm(row.account_number) != norm(invoice.service_account.account_number):
                continue
            current = latest_statement(invoice.org_unit, invoice.provider_id, invoice.currency, candidate)
            if not current or current.pk != row.statement_id:
                continue
            if row.statement.rows.filter(invoice_number__iexact=row.invoice_number).count() != 1:
                continue
            try:
                classify(invoice.provider, invoice.period_start, invoice.period_end, candidate)
            except ValidationError:
                continue
            evidence[candidate.pk] = candidate
        if len(evidence) == 1:
            cycle = next(iter(evidence.values()))
        elif len(evidence) > 1:
            cycle = unassigned_cycle(invoice.provider)
    invoice.billing_cycle = cycle
    return cycle
