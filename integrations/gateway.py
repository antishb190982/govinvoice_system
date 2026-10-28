"""Bearer-token authentication, scopes, IP allow-lists, rate limits and request logging
shared by the REST API and the MCP server."""

import functools
import hmac
import json
import time

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.request_context import client_ip, reset_context, set_context
from integrations import config
from integrations.models import ApiClient, ApiRequestLog


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error(status, code, message):
    response = JsonResponse({"error": {"code": code, "message": message}}, status=status)
    response["Cache-Control"] = "no-store"
    return response


def authenticate(request, scope):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ApiError(401, "unauthenticated", "Send 'Authorization: Bearer <token>'.")
    token = header[7:].strip()
    if not token.startswith(ApiClient.TOKEN_PREFIX) or token.count("_") < 2:
        raise ApiError(401, "unauthenticated", "Invalid token.")
    token_id, secret = token[len(ApiClient.TOKEN_PREFIX):].split("_", 1)
    client = ApiClient.objects.filter(token_id=token_id).select_related("org_unit", "service_user").first()
    if client is None or not hmac.compare_digest(client.token_hash, ApiClient.hash_secret(secret)):
        raise ApiError(401, "unauthenticated", "Invalid token.")
    if not client.is_usable:
        raise ApiError(401, "unauthenticated", "This token is revoked or expired.")
    ip = client_ip(request)
    if not config.ip_allowed(ip, client.allowed_ips):
        raise ApiError(403, "ip_not_allowed", "Requests from this address are not allowed for this client.")
    if scope and scope not in client.scopes:
        raise ApiError(403, "insufficient_scope", f"This client lacks the '{scope}' scope.")
    window = int(time.time() // 60)
    key = f"api-rate:{client.pk}:{window}"
    cache.add(key, 0, 90)
    try:
        count = cache.incr(key)
    except ValueError:
        count = 1
    if count > client.rate_limit_per_minute:
        raise ApiError(429, "rate_limited", "Too many requests; slow down.")
    ApiClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
    return client


def endpoint(scope, channel="api", methods=("GET",), operation=""):
    """Decorator: authenticate, authorise, rate-limit, log and JSON-encode an API view."""

    def wrap(view):
        @csrf_exempt
        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            started = time.monotonic()
            client, status = None, 500
            enabled_key = "MCP_ENABLED" if channel == "mcp" else "API_ENABLED"
            try:
                config.refresh()
                if not config.get(enabled_key):
                    raise ApiError(503, "disabled", f"The {channel.upper()} interface is disabled.")
                if request.method not in methods:
                    raise ApiError(405, "method_not_allowed", f"Use {' or '.join(methods)}.")
                client = authenticate(request, scope)
                tokens = set_context(client.service_user, client_ip(request))
                try:
                    response = view(request, client, *args, **kwargs)
                finally:
                    reset_context(tokens)
                if not hasattr(response, "status_code"):
                    response = JsonResponse(response, safe=False, json_dumps_params={"ensure_ascii": False})
            except ApiError as exc:
                response = error(exc.status, exc.code, exc.message)
            status = response.status_code
            response["Cache-Control"] = "no-store"
            ApiRequestLog.objects.create(client=client, channel=channel, method=request.method,
                                         path=request.path[:300], operation=operation or view.__name__,
                                         status=status, ip_address=client_ip(request),
                                         duration_ms=int((time.monotonic() - started) * 1000))
            return response

        return inner

    return wrap


def read_json(request, limit=12 * 1024 * 1024):
    if len(request.body) > limit:
        raise ApiError(413, "too_large", "Request body too large.")
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        raise ApiError(400, "invalid_json", "The body is not valid JSON.")


def client_units(client):
    """Query filter path prefix for the client's data scope (None = everything)."""
    return client.org_unit.path if client.org_unit_id else None
