from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from analytics.views import analytics
from core import views as core_views
from integrations.urls import api_patterns, console_patterns, mcp_view

admin.site.site_header = "GovBills system administration"
admin.site.site_title = "GovBills admin"

urlpatterns = [
    path("", core_views.dashboard, name="home"),
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("org/", include("org.urls")),
    path("providers/", include("providers.urls")),
    path("invoices/", include("invoices.urls")),
    path("imports/", include("imports.urls")),
    path("approvals/", include("workflow.urls")),
    path("signatures/", include("signatures.urls")),
    path("settlements/", include("settlements.urls")),
    path("analytics/", analytics, name="analytics"),
    path("ocr/", include("ocr.urls")),
    path("console/", include(console_patterns)),
    path("api/v1/", include(api_patterns)),
    path("mcp", mcp_view, name="mcp"),
    path(settings.ADMIN_URL_PATH, admin.site.urls),
]

handler403 = "core.views.handler403"
handler404 = "core.views.handler404"
