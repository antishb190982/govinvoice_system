"""Currency minor units. Amounts are stored with 3 decimals; display and bank
files use the currency's own precision (MUR 2, OMR/KWD/BHD 3, …)."""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

DECIMALS = {"MUR": 2, "USD": 2, "EUR": 2, "GBP": 2, "ZAR": 2, "INR": 2, "AED": 2, "SAR": 2,
            "OMR": 3, "KWD": 3, "BHD": 3, "JOD": 3, "TND": 3, "JPY": 0}
SYMBOLS = {"MUR": "Rs"}


def decimals(currency=None):
    return DECIMALS.get((currency or settings.DEFAULT_CURRENCY).upper(), 2)


def quantize(value, currency=None):
    places = decimals(currency)
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def fmt(value, currency=None, with_code=True):
    places = decimals(currency)
    text = f"{quantize(value, currency):,.{places}f}"
    return f"{currency} {text}" if with_code and currency else text
