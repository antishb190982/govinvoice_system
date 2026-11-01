"""Shared duplicate identities for manual, API and staged invoice intake."""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models.functions import Lower, Trim

from invoices.models import Invoice


def normalise_number(value):
    # Preserve punctuation and leading zeroes: they can be significant identifiers.
    return str(value or "").strip().lower()


def period_amount_key(provider, account_number, currency, period_start, period_end, amount):
    if not provider or not account_number or not period_start or not period_end or amount in (None, ""):
        return None
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    currency = (currency or provider.currency or settings.DEFAULT_CURRENCY).strip().upper()
    return (
        provider.pk, normalise_number(account_number), currency,
        str(period_start), str(period_end), amount,
    )


def duplicate_errors(*, provider, invoice_number, account=None, currency=None,
                     period_start=None, period_end=None, amount=None, exclude_id=None):
    """Reject an existing reference, or an identical non-cancelled bill liability.

    Paid bills remain in the comparison. A cancelled liability may be replaced
    with a NEW reference; its original provider invoice reference stays reserved.
    """
    if not provider:
        return {}
    existing = Invoice.objects.filter(provider_id=provider.pk)
    if exclude_id:
        existing = existing.exclude(pk=exclude_id)
    number = normalise_number(invoice_number)
    if number and existing.alias(_duplicate_number=Lower(Trim("invoice_number"))).filter(
            _duplicate_number=number).exists():
        return {"invoice_number": "Duplicate bill rejected: this provider invoice number is already recorded."}
    key = period_amount_key(
        provider, account.account_number if account else None, currency, period_start, period_end, amount,
    )
    if key and existing.exclude(status=Invoice.Status.CANCELLED).alias(
            _duplicate_account=Lower(Trim("service_account__account_number")),
            _duplicate_currency=Lower(Trim("currency")),
    ).filter(
        _duplicate_account=key[1], _duplicate_currency=key[2].lower(),
        period_start=period_start, period_end=period_end, amount_total=key[5],
    ).exists():
        return {"amount_total": "Duplicate bill rejected: a non-cancelled bill already has the same utility "
                "provider, account number, billing period, currency and amount, even if its invoice number differs."}
    return {}
