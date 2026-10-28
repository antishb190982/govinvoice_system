from django.contrib import admin

from org.models import Membership, OrgUnit


@admin.register(OrgUnit)
class OrgUnitAdmin(admin.ModelAdmin):
    list_display = ["indented_name", "code", "kind", "is_active"]
    search_fields = ["name", "code"]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "org_unit", "role", "valid_until"]
    list_filter = ["role"]
