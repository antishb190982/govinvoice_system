"""REST API v1 (JSON). Every endpoint is scope-checked and limited to the client's org unit."""

import base64
import binascii
import hashlib
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import JsonResponse
from django.utils import timezone

from analytics import queries
from core.files import detect_kind
from integrations.gateway import ApiError, client_units, endpoint, read_json
from invoices.models import Invoice
from invoices.services import save_invoice
from providers.models import Provider, ServiceAccount
from settlements.models import SettlementInstruction


def _invoices(client):
    qs = Invoice.objects.select_related("provider", "service_account", "org_unit")
    prefix = client_units(client)
    return qs.filter(org_unit__path__startswith=prefix) if prefix else qs


def _amt(value, currency):
    from core.money import quantize

    return str(quantize(value, currency)) if value is not None else None


def invoice_json(inv, detail=False):
    data = {
        "reference": inv.reference, "status": inv.status, "provider": inv.provider.short_code,
        "provider_name": inv.provider.name, "utility": inv.utility_type,
        "account_number": inv.service_account.account_number, "org_unit": inv.org_unit.code,
        "invoice_number": inv.invoice_number, "issue_date": inv.issue_date, "due_date": inv.due_date,
        "period_start": inv.period_start, "period_end": inv.period_end, "currency": inv.currency,
        "amount_net": _amt(inv.amount_net, inv.currency), "amount_tax": _amt(inv.amount_tax, inv.currency),
        "amount_total": _amt(inv.amount_total, inv.currency),
        "consumption": str(inv.consumption) if inv.consumption is not None else None,
        "arrears": _amt(inv.arrears, inv.currency),
        "consumption_unit": inv.consumption_unit, "flags": inv.flags, "paid_at": inv.paid_at,
        "payment_reference": inv.payment_reference or None,
    }
    if detail:
        data["approvals"] = [{"step": t.name, "status": t.status, "due_at": t.due_at, "decided_at": t.decided_at,
                              "breached": t.breached, "signed": hasattr(t, "signature")}
                             for t in inv.approval_tasks.order_by("round", "order")]
        data["must_approve_by"] = inv.must_approve_by
        data["document_sha256"] = inv.document_sha256 or None
    return data


def _page(request, qs, serialise):
    try:
        size = min(200, max(1, int(request.GET.get("page_size", 50))))
        number = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        raise ApiError(400, "invalid_parameter", "page and page_size must be integers.")
    page = Paginator(qs, size).get_page(number)
    return {"count": page.paginator.count, "page": page.number, "pages": page.paginator.num_pages,
            "results": [serialise(o) for o in page]}


def _date(value, name):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "invalid_parameter", f"{name} must be YYYY-MM-DD.")


@endpoint(scope=None, operation="health")
def health(request, client):
    return {"status": "ok", "time": timezone.now(), "client": client.name}


@endpoint(scope="invoices:read", methods=("GET", "POST"), operation="invoices")
def invoices(request, client):
    if request.method == "POST":
        return create_invoice(request, client)
    qs = _invoices(client)
    g = request.GET
    if g.get("status"):
        qs = qs.filter(status__in=g["status"].split(","))
    if g.get("provider"):
        qs = qs.filter(provider__short_code__iexact=g["provider"])
    if g.get("org_unit"):
        qs = qs.filter(org_unit__code__iexact=g["org_unit"])
    if g.get("due_before"):
        qs = qs.filter(due_date__lte=_date(g["due_before"], "due_before"))
    if g.get("due_after"):
        qs = qs.filter(due_date__gte=_date(g["due_after"], "due_after"))
    if g.get("updated_since"):
        qs = qs.filter(updated_at__date__gte=_date(g["updated_since"], "updated_since"))
    return _page(request, qs.order_by("due_date", "id"), invoice_json)


def create_invoice(request, client):
    """Provider e-invoice intake: creates a DRAFT invoice for the clerks to check and submit."""
    if "invoices:write" not in client.scopes:
        raise ApiError(403, "insufficient_scope", "This client lacks the 'invoices:write' scope.")
    body = read_json(request)
    required = ["provider", "account_number", "invoice_number", "issue_date", "due_date", "amount_total"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        raise ApiError(400, "missing_fields", "Missing: " + ", ".join(missing))
    provider = Provider.objects.filter(short_code__iexact=body["provider"], is_active=True).first()
    if not provider:
        raise ApiError(400, "unknown_provider", "Unknown provider code.")
    account = ServiceAccount.objects.filter(provider=provider, account_number__iexact=body["account_number"],
                                            is_active=True).select_related("org_unit").first()
    if not account:
        raise ApiError(400, "unknown_account", "This account is not registered for the provider.")
    prefix = client_units(client)
    if prefix and not account.org_unit.path.startswith(prefix):
        raise ApiError(403, "out_of_scope", "The account belongs to a unit outside this client's scope.")
    try:
        inv = Invoice(service_account=account, invoice_number=str(body["invoice_number"])[:60],
                      issue_date=_date(body["issue_date"], "issue_date"), due_date=_date(body["due_date"], "due_date"),
                      period_start=_date(body.get("period_start"), "period_start"),
                      period_end=_date(body.get("period_end"), "period_end"),
                      currency=str(body.get("currency") or "")[:3].upper(),
                      amount_net=Decimal(str(body.get("amount_net") or 0)), amount_tax=Decimal(str(body.get("amount_tax") or 0)),
                      amount_total=Decimal(str(body["amount_total"])),
                      consumption=Decimal(str(body["consumption"])) if body.get("consumption") not in (None, "") else None,
                      source=Invoice.Source.API, notes=f"Received via API client {client.name}")
    except (InvalidOperation, TypeError):
        raise ApiError(400, "invalid_amount", "Amounts must be decimal numbers.")
    if body.get("document_base64"):
        try:
            content = base64.b64decode(body["document_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, "invalid_document", "document_base64 is not valid base64.")
        kind = detect_kind(content[:4096], body.get("document_name") or "invoice.pdf")
        if kind not in ("pdf", "png", "jpg", "tif"):
            raise ApiError(400, "invalid_document", "The document must be a PDF or an image.")
        name = (body.get("document_name") or f"{inv.invoice_number}.{kind}")[:120]
        inv.document.save(name, ContentFile(content), save=False)
        inv.document_name = name
        inv.document_sha256 = hashlib.sha256(content).hexdigest()
    try:
        save_invoice(inv, client.service_user, audit_action="invoice.received_api")
    except ValidationError as exc:
        if inv.document:
            inv.document.delete(save=False)
        messages = [m for msgs in getattr(exc, "message_dict", {"": exc.messages}).values() for m in msgs]
        raise ApiError(422, "rejected", " ".join(messages))
    response = JsonResponse(invoice_json(inv, detail=True), status=201)
    response["Location"] = f"/api/v1/invoices/{inv.reference}"
    return response


@endpoint(scope="invoices:read", operation="invoice")
def invoice(request, client, reference):
    inv = _invoices(client).filter(reference=reference).first()
    if not inv:
        raise ApiError(404, "not_found", "No such invoice in this client's scope.")
    return invoice_json(inv, detail=True)


@endpoint(scope="providers:read", operation="providers")
def providers(request, client):
    out = []
    for p in Provider.objects.filter(is_active=True):
        accounts = p.accounts.filter(is_active=True)
        prefix = client_units(client)
        if prefix:
            accounts = accounts.filter(org_unit__path__startswith=prefix)
        out.append({"code": p.short_code, "name": p.name, "utility": p.utility_type, "currency": p.currency,
                    "payment_terms_days": p.payment_terms_days,
                    "accounts": [{"account_number": a.account_number, "meter_number": a.meter_number,
                                  "org_unit": a.org_unit.code, "premises": a.premises}
                                 for a in accounts.select_related("org_unit")]})
    return {"results": out}


def settlement_json(si, detail=False):
    data = {"reference": si.reference, "status": si.status, "org_unit": si.org_unit.code, "currency": si.currency,
            "total_amount": _amt(si.total_amount, si.currency), "pay_by": si.pay_by,
            "requested_execution_date": si.requested_execution_date, "executed_at": si.executed_at,
            "bank_reference": si.bank_reference or None}
    if detail:
        data["lines"] = [{"provider": li.provider.short_code, "amount": _amt(li.amount, si.currency), "invoices": li.invoice_count,
                          "earliest_due": li.earliest_due, "end_to_end_id": li.end_to_end_id}
                         for li in si.lines.select_related("provider")]
    return data


@endpoint(scope="settlements:read", operation="settlements")
def settlements(request, client):
    qs = SettlementInstruction.objects.select_related("org_unit")
    prefix = client_units(client)
    if prefix:
        qs = qs.filter(org_unit__path__startswith=prefix)
    if request.GET.get("status"):
        qs = qs.filter(status__in=request.GET["status"].split(","))
    return _page(request, qs, settlement_json)


@endpoint(scope="settlements:read", operation="settlement")
def settlement(request, client, reference):
    qs = SettlementInstruction.objects.select_related("org_unit")
    prefix = client_units(client)
    if prefix:
        qs = qs.filter(org_unit__path__startswith=prefix)
    si = qs.filter(reference=reference).first()
    if not si:
        raise ApiError(404, "not_found", "No such settlement instruction in this client's scope.")
    return settlement_json(si, detail=True)


@endpoint(scope="analytics:read", operation="analytics.monthly")
def analytics_monthly(request, client):
    try:
        months = min(36, max(1, int(request.GET.get("months", 12))))
    except ValueError:
        raise ApiError(400, "invalid_parameter", "months must be an integer.")
    qs = _invoices(client)
    if request.GET.get("currency"):
        qs = qs.filter(currency=request.GET["currency"].upper())
    return queries.monthly_spend(qs, months)


@endpoint(scope="invoices:read", operation="dues")
def dues(request, client):
    try:
        days = min(90, max(1, int(request.GET.get("days", 14))))
    except ValueError:
        raise ApiError(400, "invalid_parameter", "days must be an integer.")
    horizon = timezone.localdate() + timedelta(days=days)
    rows = _invoices(client).filter(status__in=Invoice.UNPAID, due_date__lte=horizon) \
        .values("provider__short_code", "provider__name", "currency").annotate(total=Sum("amount_total")).order_by("provider__name")
    return {"horizon": horizon, "results": [{"provider": r["provider__short_code"], "provider_name": r["provider__name"],
                                             "currency": r["currency"], "amount_due": _amt(r["total"], r["currency"])} for r in rows]}


OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "GovBills API", "version": "1.0",
             "description": "Utility invoice data for government systems. Authenticate with 'Authorization: Bearer <token>' "
                            "issued in the admin console. Data is limited to the client's organisation unit."},
    "servers": [{"url": "/api/v1"}],
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "security": [{"bearer": []}],
    "paths": {
        "/health": {"get": {"summary": "Check the token and service", "responses": {"200": {"description": "OK"}}}},
        "/invoices": {
            "get": {"summary": "List invoices (scope invoices:read)", "parameters": [
                {"name": n, "in": "query", "schema": {"type": "string"}} for n in
                ("status", "provider", "org_unit", "due_before", "due_after", "updated_since", "page", "page_size")],
                "responses": {"200": {"description": "Page of invoices"}}},
            "post": {"summary": "Submit a provider e-invoice as a draft (scope invoices:write)",
                     "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": [
                         "provider", "account_number", "invoice_number", "issue_date", "due_date", "amount_total"],
                         "properties": {k: {"type": "string"} for k in (
                             "provider", "account_number", "invoice_number", "issue_date", "due_date", "period_start",
                             "period_end", "currency", "amount_net", "amount_tax", "amount_total", "consumption",
                             "document_base64", "document_name")}}}}},
                     "responses": {"201": {"description": "Created"}, "422": {"description": "Rejected (e.g. duplicate)"}}}},
        "/invoices/{reference}": {"get": {"summary": "Invoice with approval trail", "parameters": [
            {"name": "reference", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Invoice"}, "404": {"description": "Not found"}}}},
        "/dues": {"get": {"summary": "Unpaid totals per provider due within N days", "responses": {"200": {"description": "OK"}}}},
        "/providers": {"get": {"summary": "Providers and service accounts (scope providers:read)", "responses": {"200": {"description": "OK"}}}},
        "/settlements": {"get": {"summary": "Settlement instructions (scope settlements:read)", "responses": {"200": {"description": "OK"}}}},
        "/settlements/{reference}": {"get": {"summary": "Instruction with per-provider lines", "responses": {"200": {"description": "OK"}}}},
        "/analytics/monthly": {"get": {"summary": "Monthly spend by utility (scope analytics:read)", "responses": {"200": {"description": "OK"}}}},
    },
}


def openapi(request):
    return JsonResponse(OPENAPI)
