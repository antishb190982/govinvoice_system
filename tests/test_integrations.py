"""Integration gateway (REST API, MCP), runtime configuration, e-mail/SMS channels and admin console."""

import io
import json
from unittest import mock

import pytest
from django.conf import settings
from django.core import mail
from django.urls import reverse

from integrations import config, sms
from integrations.models import ApiClient, ApiRequestLog, ConfigEntry, OutboundMessage
from tests.conftest import make_user
from tests.test_views import sign_in


@pytest.fixture
def api_client_factory(people, tree):
    def make(scopes=("invoices:read",), unit=None, **extra):
        service = make_user(f"api{ApiClient.objects.count()}")
        client = ApiClient(name=f"client{ApiClient.objects.count()}", scopes=list(scopes), org_unit=unit,
                           service_user=service, created_by=people["admin"], **extra)
        token = client.issue_token()
        client.save()
        return client, token

    return make


def call(client, token, path, method="get", **kwargs):
    return getattr(client, method)(path, HTTP_AUTHORIZATION=f"Bearer {token}", **kwargs)


def test_api_auth_scope_and_logging(client, api_client_factory, make_invoice):
    make_invoice()
    c, token = api_client_factory()
    assert call(client, token, "/api/v1/health").status_code == 200
    assert client.get("/api/v1/invoices").status_code == 401
    assert call(client, token + "x", "/api/v1/invoices").status_code == 401
    assert call(client, token, "/api/v1/settlements").status_code == 403  # missing scope
    r = call(client, token, "/api/v1/invoices")
    assert r.status_code == 200 and r.json()["count"] == 1
    assert ApiRequestLog.objects.filter(client=c, status=200).count() == 2
    assert r["Cache-Control"] == "no-store"


def test_api_unit_scope(client, api_client_factory, make_invoice, tree):
    make_invoice()
    _, token = api_client_factory(unit=tree["moe"])
    assert call(client, token, "/api/v1/invoices").json()["count"] == 0
    inv = make_invoice()
    assert call(client, token, f"/api/v1/invoices/{inv.reference}").status_code == 404


def test_api_ip_allowlist_expiry_and_rate_limit(client, api_client_factory):
    _, token = api_client_factory(allowed_ips="10.9.0.0/16")
    assert call(client, token, "/api/v1/health").status_code == 403
    c, token = api_client_factory(rate_limit_per_minute=2)
    codes = [call(client, token, "/api/v1/health").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    c.revoked_at = c.created_at
    c.save()
    assert call(client, token, "/api/v1/health").status_code == 401


def test_api_invoice_intake(client, api_client_factory, account):
    _, token = api_client_factory(scopes=("invoices:read", "invoices:write"))
    body = {"provider": "PWR", "account_number": "ACC-1", "invoice_number": "E-1001", "issue_date": "2026-10-01",
            "due_date": "2026-11-15", "amount_net": "1000.00", "amount_tax": "150.00", "amount_total": "1150.00"}
    r = call(client, token, "/api/v1/invoices", "post", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 201, r.content
    data = r.json()
    assert data["status"] == "draft" and data["amount_total"] == "1150.00"
    r = call(client, token, "/api/v1/invoices", "post", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 422  # duplicate provider invoice number
    _, read_only = api_client_factory()
    r = call(client, read_only, "/api/v1/invoices", "post", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 403


def test_openapi_is_published(client, db):
    spec = client.get("/api/v1/openapi.json").json()
    assert spec["openapi"].startswith("3.") and "/invoices" in spec["paths"]


def _rpc(client, token, method, params=None, rpc_id=1, **headers):
    return call(client, token, "/mcp", "post", content_type="application/json",
                data=json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}), **headers)


def test_mcp_protocol_and_tools(client, api_client_factory, make_invoice):
    inv = make_invoice()
    _, token = api_client_factory(scopes=("mcp",))
    init = _rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "test", "version": "1"}}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    note = call(client, token, "/mcp", "post", content_type="application/json",
                data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert note.status_code == 202
    tools = {t["name"] for t in _rpc(client, token, "tools/list").json()["result"]["tools"]}
    assert {"search_invoices", "get_invoice", "upcoming_dues", "sla_breaches"} <= tools
    res = _rpc(client, token, "tools/call", {"name": "get_invoice", "arguments": {"reference": inv.reference}}).json()
    assert res["result"]["isError"] is False and res["result"]["structuredContent"]["reference"] == inv.reference
    res = _rpc(client, token, "tools/call", {"name": "get_invoice", "arguments": {"reference": "nope"}}).json()
    assert res["result"]["isError"] is True
    assert _rpc(client, token, "approve_everything").json()["error"]["code"] == -32601


def test_mcp_requires_scope_and_valid_origin(client, api_client_factory):
    _, no_mcp = api_client_factory(scopes=("invoices:read",))
    assert _rpc(client, no_mcp, "tools/list").status_code == 403
    _, token = api_client_factory(scopes=("mcp",))
    assert _rpc(client, token, "tools/list", HTTP_ORIGIN="https://evil.example").status_code == 403
    assert client.get("/mcp", HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 405


def test_disabled_interface_returns_503(client, api_client_factory, people):
    _, token = api_client_factory()
    config.save({"API_ENABLED": False}, people["admin"])
    try:
        assert call(client, token, "/api/v1/health").status_code == 503
    finally:
        config.save({"API_ENABLED": True}, people["admin"])


def test_runtime_config_overrides_and_encrypts_secrets(people, db):
    config.save({"DUE_SOON_DAYS": 11, "SMTP_PASSWORD": "s3cret!"}, people["admin"])
    assert settings.DUE_SOON_DAYS == 11 and config.get("SMTP_PASSWORD") == "s3cret!"
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT secret_value FROM integrations_configentry WHERE key = %s", ["SMTP_PASSWORD"])
        assert "s3cret" not in cur.fetchone()[0]
    config.save({"SMTP_PASSWORD": ""}, people["admin"])  # blank keeps the stored secret
    assert config.get("SMTP_PASSWORD") == "s3cret!"
    ConfigEntry.objects.filter(key="DUE_SOON_DAYS").delete()
    config.refresh(force=True)
    assert settings.DUE_SOON_DAYS == config.default("DUE_SOON_DAYS")


def test_email_backend_logs_and_skips_when_disabled(settings, db):
    settings.EMAIL_BACKEND = "integrations.mail.ConfigurableEmailBackend"
    with mock.patch("sys.stdout", new=io.StringIO()):
        mail.send_mail("Hello", "Body", None, ["x@example.gov"])
    assert OutboundMessage.objects.get(channel="email").status == "skipped"


def test_sms_http_gateway(people, db):
    config.save({"SMS_ENABLED": True, "SMS_PROVIDER": "http", "SMS_URL": "https://sms.example.mu/send",
                 "SMS_TOKEN": "tok", "SMS_SENDER": "GOVMU"}, people["admin"])
    captured = {}

    class Resp:
        status = 200

        def read(self, n):
            return b'{"id": "msg-1"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        captured["url"], captured["body"], captured["auth"] = req.full_url, json.loads(req.data), req.headers["Authorization"]
        return Resp()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        assert sms.send("5 712 3456", "SLA breached") is True
    assert captured["body"] == {"to": "+23057123456", "message": "SLA breached", "from": "GOVMU"}
    assert captured["auth"] == "Bearer tok"
    msg = OutboundMessage.objects.get(channel="sms")
    assert msg.status == "sent" and "3456" not in msg.recipient[:-3] and msg.recipient.endswith("456")
    from django.core.exceptions import ValidationError
    from django.test import override_settings

    with pytest.raises(ValidationError, match="HTTPS"):
        config.save({"SMS_URL": "http://insecure.example/send"}, people["admin"])
    # Legacy environment values must still fail closed in the transport.
    with override_settings(SMS_URL="http://insecure.example/send"), mock.patch("urllib.request.urlopen", fake_urlopen):
        assert sms.send("57123456", "x") is False  # HTTPS required


def test_critical_notifications_go_by_sms(people, db, django_capture_on_commit_callbacks):
    from core.notify import notify

    user = people["approver"]
    user.phone = "57123456"
    user.save()
    config.save({"SMS_ENABLED": True, "SMS_URL": "https://sms.example.mu/send", "SMS_MIN_LEVEL": "critical"}, people["admin"])
    with mock.patch("integrations.sms.send") as send, django_capture_on_commit_callbacks(execute=True):
        notify([user], "Info only", level="info")
        notify([user], "SLA breached", level="critical")
    assert send.call_count == 1 and "SLA breached" in send.call_args[0][1]


@pytest.mark.parametrize("name", ["console:overview", "console:gateways", "console:settings", "console:email", "console:sms",
                                  "console:api", "console:logs", "console:holidays"])
def test_console_pages(client, people, name):
    root = make_user("root", superuser=True)
    sign_in(client, root)
    assert client.get(reverse(name)).status_code == 200
    client.post(reverse("accounts:logout"))
    sign_in(client, people["admin"])
    assert client.get(reverse(name)).status_code == 403


def test_console_creates_api_client_and_shows_token_once(client, people, tree):
    root = make_user("root", superuser=True)
    sign_in(client, root)
    r = client.post(reverse("console:api"), {"name": "Treasury ERP", "scopes": ["invoices:read", "mcp"],
                                             "org_unit": tree["moh"].pk, "rate_limit_per_minute": 60, "allowed_ips": ""})
    assert r.status_code == 200 and b"gbk_" in r.content
    c = ApiClient.objects.get(name="Treasury ERP")
    assert c.token_hash and c.service_user.is_active is False
    token = r.content.split(b"gbk_")[1].split(b"<")[0].decode()
    assert client.get("/api/v1/health", HTTP_AUTHORIZATION=f"Bearer gbk_{token}").status_code == 200


def test_console_settings_save(client, people):
    root = make_user("root", superuser=True)
    sign_in(client, root)
    r = client.post(reverse("console:settings_group", args=["working-calendar"]),
                    {"WORK_WEEK_DAYS": ["0", "1", "2", "3", "4"], "WORK_DAY_START_HOUR": "9", "WORK_DAY_END_HOUR": "16"})
    assert r.status_code == 302
    assert settings.WORK_DAY_START_HOUR == 9
    r = client.post(reverse("console:settings_group", args=["working-calendar"]),
                    {"WORK_WEEK_DAYS": ["0"], "WORK_DAY_START_HOUR": "17", "WORK_DAY_END_HOUR": "9"})
    assert r.status_code == 200 and b"must start before it ends" in r.content
    ConfigEntry.objects.all().delete()
    config.refresh(force=True)
