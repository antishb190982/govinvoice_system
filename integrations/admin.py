from django.contrib import admin

from integrations.models import ApiRequestLog, OutboundMessage


@admin.register(ApiRequestLog)
class ApiRequestLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "client", "channel", "method", "path", "status"]
    list_filter = ["channel", "status"]

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(OutboundMessage)
class OutboundMessageAdmin(admin.ModelAdmin):
    list_display = ["created_at", "channel", "recipient", "subject", "status"]
    list_filter = ["channel", "status"]

    def has_change_permission(self, request, obj=None):
        return False
