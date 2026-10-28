from django.urls import path

from integrations import api, console, mcp

console_patterns = ([
    path("", console.overview, name="overview"),
    path("settings/", console.settings_view, name="settings"),
    path("settings/<slug:group>/", console.settings_view, name="settings_group"),
    path("email/", console.email_view, name="email"),
    path("sms/", console.sms_view, name="sms"),
    path("api-clients/", console.api_clients, name="api"),
    path("api-clients/<int:pk>/<str:action>/", console.api_client_action, name="api_action"),
    path("logs/", console.logs, name="logs"),
    path("holidays/", console.holidays, name="holidays"),
], "console")

api_patterns = ([
    path("health", api.health, name="health"),
    path("openapi.json", api.openapi, name="openapi"),
    path("invoices", api.invoices, name="invoices"),
    path("invoices/<str:reference>", api.invoice, name="invoice"),
    path("dues", api.dues, name="dues"),
    path("providers", api.providers, name="providers"),
    path("settlements", api.settlements, name="settlements"),
    path("settlements/<str:reference>", api.settlement, name="settlement"),
    path("analytics/monthly", api.analytics_monthly, name="analytics_monthly"),
], "api")

mcp_view = mcp.server
