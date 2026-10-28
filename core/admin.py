from django.contrib import admin

from core.models import AuditEvent, Holiday


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ["date", "name"]


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ["sequence", "occurred_at", "actor_label", "action", "object_type", "object_id"]
    list_filter = ["action"]
    search_fields = ["actor_label", "object_id", "action"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
