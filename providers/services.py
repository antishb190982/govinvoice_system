import re

from django.db.models import Q

from providers.models import Provider, ServiceAccount


def _variants(value):
    value = re.sub(r"[\s]", "", str(value or ""))
    if not value:
        return None
    return value


def find_account(provider, account_number=None, service_number=None):
    """Find a provider's active service account by billing account number (leading-zero
    tolerant, e.g. CEB 000118902086 = 118902086) or by its line / meter number."""
    qs = ServiceAccount.objects.filter(provider=provider).select_related("org_unit", "provider")
    for value in (_variants(account_number), _variants(service_number)):
        if not value:
            continue
        found = qs.filter(account_number__iexact=value).first()
        if found:
            return found
        if value.isdigit():
            stripped = value.lstrip("0") or "0"
            found = qs.filter(account_number__regex=rf"^0*{re.escape(stripped)}$").first()
            if found:
                return found
        found = qs.filter(Q(meter_number__iexact=value) | Q(meter_number__iexact=value.lstrip("#"))).first()
        if found:
            return found
    return None


def provider_from_invoice_number(number):
    """Recognise a provider from the format of its bill numbers (Provider.invoice_number_pattern)."""
    if not number:
        return None
    for provider in Provider.objects.filter(is_active=True).exclude(invoice_number_pattern=""):
        try:
            if re.search(provider.invoice_number_pattern, str(number).strip()):
                return provider
        except re.error:
            continue
    return None
