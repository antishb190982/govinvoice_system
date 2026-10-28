from django.core.management.base import BaseCommand

from ocr.profiles import PROFILES
from providers.models import Provider

TAX_NUMBERS = {"MT": "VAT20070778", "CEB": "VAT22000591"}


class Command(BaseCommand):
    help = "Fill bill-number patterns and VAT numbers of providers that have a built-in reading profile."

    def handle(self, *args, **options):
        for profile in PROFILES:
            for code in profile["provider_codes"]:
                provider = Provider.objects.filter(short_code=code).first()
                if not provider:
                    continue
                changed = []
                if not provider.invoice_number_pattern and profile["invoice_number_pattern"]:
                    provider.invoice_number_pattern = profile["invoice_number_pattern"]
                    changed.append("bill-number pattern")
                if not provider.tax_number and code in TAX_NUMBERS:
                    provider.tax_number = TAX_NUMBERS[code]
                    changed.append("VAT number")
                if changed:
                    provider.save()
                self.stdout.write(f"{provider.name}: {', '.join(changed) or 'already set'} ({profile['name']})")
