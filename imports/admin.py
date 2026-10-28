from django.contrib import admin

from imports.models import ImportTemplate


@admin.register(ImportTemplate)
class ImportTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "provider", "kind", "is_active"]
