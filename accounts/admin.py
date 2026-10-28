from django.contrib import admin

from accounts.models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["username", "full_name", "email", "is_active", "is_superuser", "totp_confirmed_at"]
    search_fields = ["username", "full_name", "email"]
    fields = ["username", "full_name", "email", "job_title", "employee_id", "phone", "is_active", "is_superuser"]
