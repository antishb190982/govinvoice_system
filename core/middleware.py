from core.request_context import client_ip, reset_context, set_context

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.user.is_authenticated if hasattr(request, "user") else False:
            # Financial pages must not linger in shared-browser caches.
            response.headers["Cache-Control"] = "no-store"
        return response


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tokens = set_context(getattr(request, "user", None), client_ip(request))
        from core.provider_scope import activate, reset
        from django.http import HttpResponseBadRequest
        try:
            provider_token = activate(request)
        except ValueError as exc:
            reset_context(tokens)
            return HttpResponseBadRequest(str(exc))
        try:
            return self.get_response(request)
        finally:
            reset(provider_token)
            reset_context(tokens)


class PendingMigrationsMiddleware:
    """
    After an update, the code may expect database columns that `migrate` has not
    created yet. Instead of an opaque 500 after sign-in, explain what to run.
    Checked once per process (and re-checked every minute while pending).
    """

    _state = {"pending": None, "checked": 0.0}

    def __init__(self, get_response):
        self.get_response = get_response

    @classmethod
    def pending(cls):
        import time

        now = time.monotonic()
        if cls._state["pending"] == 0 or (cls._state["pending"] is not None and now - cls._state["checked"] < 60):
            return cls._state["pending"]
        from django.db import DEFAULT_DB_ALIAS, connections
        from django.db.migrations.executor import MigrationExecutor

        try:
            executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
            cls._state["pending"] = len(executor.migration_plan(executor.loader.graph.leaf_nodes()))
        except Exception:
            cls._state["pending"] = None  # database unreachable: let the normal error handling report it
        cls._state["checked"] = now
        return cls._state["pending"]

    def __call__(self, request):
        count = self.pending()
        if count and not request.path.startswith("/static/"):
            from django.http import HttpResponse

            body = ("<!doctype html><meta charset='utf-8'><title>Database update needed</title>"
                    "<body>"
                    "<h1>Database update needed</h1>"
                    f"<p>This version of the application needs {count} database migration(s) that have not been applied yet.</p>"
                    "<p>An administrator should run:</p><pre>python manage.py migrate</pre>"
                    "<p>then restart the application. (Docker: <code>docker compose up -d --build</code> does this automatically.)</p>")
            return HttpResponse(body, status=503, headers={"Retry-After": "60"})
        return self.get_response(request)
