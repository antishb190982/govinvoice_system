from django.contrib import admin

from ocr.models import OcrTemplate


@admin.register(OcrTemplate)
class OcrTemplateAdmin(admin.ModelAdmin):
    list_display = ["provider", "learned_from", "is_active", "updated_at"]
