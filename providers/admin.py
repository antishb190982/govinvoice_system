from django.contrib import admin

from providers.models import Provider, ServiceAccount


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ["name", "short_code", "utility_type", "is_active"]
    # Bank details change only through the dual-control screen.
    exclude = ["bank_name", "bank_bic", "bank_iban", "bank_account_name"]


@admin.register(ServiceAccount)
class ServiceAccountAdmin(admin.ModelAdmin):
    list_display = ["provider", "account_number", "org_unit", "premises", "is_active"]
    search_fields = ["account_number", "meter_number", "premises"]
