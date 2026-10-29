from django.urls import path

from integrations import api, console, mcp
from integrations import inbound, inbound_admin

console_patterns = ([
    path("incoming/", inbound_admin.incoming, name="incoming"),
    path("incoming/<int:pk>/", inbound_admin.incoming, name="incoming_mailbox"),
    path("incoming/<int:pk>/rules/<int:rule_pk>/", inbound_admin.incoming, name="incoming_rule"),
    path("incoming/<int:pk>/rules/<int:rule_pk>/toggle/", inbound_admin.toggle_sender, name="incoming_rule_toggle"),
    path("incoming/messages/<int:pk>/", inbound_admin.delivery, name="incoming_delivery"),
    path("incoming/messages/<int:pk>/retry/", inbound_admin.retry, name="incoming_retry"),
    path("incoming/messages/<int:pk>/source/", inbound_admin.download, name="incoming_download"),
    path("", console.overview, name="overview"),
    path("gateways/", console.gateways, name="gateways"),
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
    path("mail/receive", inbound.receive, name="mail_receive"),
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
