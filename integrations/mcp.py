"""
Model Context Protocol server (Streamable HTTP transport, JSON responses).

AI assistants connect to POST /mcp with a bearer token that carries the
"mcp" scope. Tools are read-only by design: an assistant can answer questions
about invoices, due dates, SLA breaches and spend, but cannot approve, sign
or pay anything. Data is limited to the token's organisation unit.
"""

import json
from datetime import timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from analytics import queries
from integrations.api import _amt, _invoices, invoice_json, settlement_json
from integrations.gateway import client_units, endpoint
from invoices.models import Invoice
from settlements.models import SettlementInstruction
from workflow.models import ApprovalTask

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "govbills", "title": "GovBills utility invoices", "version": "1.0.0"}


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "search_invoices", "title": "Search invoices",
     "description": "Find utility invoices by status, provider code, unit code, due-date range or text.",
     "inputSchema": _schema({"status": {"type": "string", "description": "e.g. in_approval, approved, paid, or 'unpaid'"},
                             "provider": {"type": "string"}, "org_unit": {"type": "string"},
                             "due_before": {"type": "string", "format": "date"}, "due_after": {"type": "string", "format": "date"},
                             "text": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
     "annotations": {"readOnlyHint": True}},
    {"name": "get_invoice", "title": "Get invoice",
     "description": "Full details of one invoice, including its approval steps, by reference (e.g. INV-2026-000123).",
     "inputSchema": _schema({"reference": {"type": "string"}}, ["reference"]), "annotations": {"readOnlyHint": True}},
    {"name": "upcoming_dues", "title": "Amounts due soon",
     "description": "Unpaid totals per utility provider falling due within the next N days, with what is still in approval.",
     "inputSchema": _schema({"days": {"type": "integer", "minimum": 1, "maximum": 90}}), "annotations": {"readOnlyHint": True}},
    {"name": "sla_breaches", "title": "Approval SLA breaches",
     "description": "Approval steps that are past their SLA deadline, most overdue first.",
     "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}), "annotations": {"readOnlyHint": True}},
    {"name": "spend_summary", "title": "Monthly spend",
     "description": "Monthly utility spend by utility type for the last N months.",
     "inputSchema": _schema({"months": {"type": "integer", "minimum": 1, "maximum": 36}, "currency": {"type": "string"}}),
     "annotations": {"readOnlyHint": True}},
    {"name": "list_settlements", "title": "Settlement instructions",
     "description": "Payment instructions with their status, totals and pay-by dates.",
     "inputSchema": _schema({"status": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
     "annotations": {"readOnlyHint": True}},
]


def _limit(args, default=20, maximum=100):
    try:
        return max(1, min(maximum, int(args.get("limit", default))))
    except (TypeError, ValueError):
        return default


def tool_search_invoices(client, args):
    qs = _invoices(client)
    if args.get("status") == "unpaid":
        qs = qs.filter(status__in=Invoice.UNPAID)
    elif args.get("status"):
        qs = qs.filter(status=args["status"])
    if args.get("provider"):
        qs = qs.filter(provider__short_code__iexact=args["provider"])
    if args.get("org_unit"):
        qs = qs.filter(org_unit__code__iexact=args["org_unit"])
    if args.get("due_before"):
        qs = qs.filter(due_date__lte=args["due_before"])
    if args.get("due_after"):
        qs = qs.filter(due_date__gte=args["due_after"])
    if args.get("text"):
        t = args["text"]
        qs = qs.filter(Q(reference__icontains=t) | Q(invoice_number__icontains=t) | Q(provider__name__icontains=t)
                       | Q(service_account__account_number__icontains=t))
    total = qs.count()
    return {"total": total, "invoices": [invoice_json(i) for i in qs.order_by("due_date")[:_limit(args)]]}


def tool_get_invoice(client, args):
    inv = _invoices(client).filter(reference=args.get("reference", "")).first()
    if not inv:
        raise ValueError("No such invoice in your scope.")
    return invoice_json(inv, detail=True)


def tool_upcoming_dues(client, args):
    days = max(1, min(90, int(args.get("days", 14))))
    horizon = timezone.localdate() + timedelta(days=days)
    qs = _invoices(client).filter(status__in=Invoice.UNPAID, due_date__lte=horizon)
    rows = qs.values("provider__name", "currency").annotate(
        total=Sum("amount_total"), in_approval=Sum("amount_total", filter=Q(status=Invoice.Status.IN_APPROVAL)),
        approved=Sum("amount_total", filter=Q(status=Invoice.Status.APPROVED))).order_by("provider__name")
    return {"horizon": horizon, "providers": [
        {"provider": r["provider__name"], "currency": r["currency"], "total_due": _amt(r["total"], r["currency"]),
         "still_in_approval": _amt(r["in_approval"] or 0, r["currency"]),
         "approved_not_instructed": _amt(r["approved"] or 0, r["currency"])} for r in rows]}


def tool_sla_breaches(client, args):
    qs = ApprovalTask.objects.filter(status=ApprovalTask.Status.PENDING, due_at__lt=timezone.now()) \
        .select_related("invoice__provider", "invoice__org_unit")
    prefix = client_units(client)
    if prefix:
        qs = qs.filter(invoice__org_unit__path__startswith=prefix)
    return {"breaches": [{"invoice": t.invoice.reference, "provider": t.invoice.provider.name, "step": t.name,
                          "role": t.role, "due_at": t.due_at, "escalated": bool(t.escalated_at),
                          "invoice_due_date": t.invoice.due_date, "amount": _amt(t.invoice.amount_total, t.invoice.currency)}
                         for t in qs.order_by("due_at")[:_limit(args)]]}


def tool_spend_summary(client, args):
    months = max(1, min(36, int(args.get("months", 12))))
    qs = _invoices(client)
    if args.get("currency"):
        qs = qs.filter(currency=args["currency"].upper())
    return queries.monthly_spend(qs, months)


def tool_list_settlements(client, args):
    qs = SettlementInstruction.objects.select_related("org_unit")
    prefix = client_units(client)
    if prefix:
        qs = qs.filter(org_unit__path__startswith=prefix)
    if args.get("status"):
        qs = qs.filter(status=args["status"])
    return {"settlements": [settlement_json(s) for s in qs[:_limit(args, 10, 50)]]}


HANDLERS = {t["name"]: globals()[f"tool_{t['name']}"] for t in TOOLS}


def _result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(rpc_id, code, message):
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def handle(client, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
    method, rpc_id, params = message["method"], message.get("id"), message.get("params") or {}
    if rpc_id is None:  # notification (e.g. notifications/initialized): no response
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(rpc_id, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": SERVER_INFO,
                                "instructions": "Read-only access to government utility invoices, due dates, approval "
                                                "SLAs and settlement instructions. Amounts are strings in the invoice currency."})
    if method == "ping":
        return _result(rpc_id, {})
    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(rpc_id, -32602, f"Unknown tool: {name}")
        try:
            data = handler(client, args)
        except (ValueError, TypeError) as exc:
            return _result(rpc_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        text = json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False)
        return _result(rpc_id, {"content": [{"type": "text", "text": text}],
                                "structuredContent": json.loads(text), "isError": False})
    return _error(rpc_id, -32601, f"Method not found: {method}")


@endpoint(scope="mcp", channel="mcp", methods=("POST",), operation="mcp")
def server(request, client):
    origin = request.headers.get("Origin")
    if origin:
        # Streamable HTTP servers must reject foreign browser origins (DNS-rebinding protection).
        from urllib.parse import urlparse

        from django.conf import settings

        host = urlparse(origin).hostname or ""
        allowed = {urlparse(settings.SITE_URL).hostname, *settings.ALLOWED_HOSTS}
        if host not in allowed:
            return JsonResponse(_error(None, -32000, "Origin not allowed"), status=403)
    try:
        payload = json.loads(request.body or b"null")
    except ValueError:
        return JsonResponse(_error(None, -32700, "Parse error"), status=400)
    if isinstance(payload, list):
        responses = [r for r in (handle(client, m) for m in payload) if r is not None]
        return JsonResponse(responses, safe=False, json_dumps_params={"cls": DjangoJSONEncoder}) if responses \
            else HttpResponse(status=202)
    response = handle(client, payload)
    if response is None:
        return HttpResponse(status=202)
    return JsonResponse(response, encoder=DjangoJSONEncoder, json_dumps_params={"ensure_ascii": False})

