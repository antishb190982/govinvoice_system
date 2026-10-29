"""Read-only readiness and initial-setup routing; no production DDL or seeding."""

import time

from django.apps import apps
from django.db import connection
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

_schema_cache = {"ready": False, "checked": 0.0}


def schema_ready():
    now = time.monotonic()
    if now - _schema_cache["checked"] < (60 if _schema_cache["ready"] else 3):
        return _schema_cache["ready"]
    tables = set(connection.introspection.table_names())
    required = {
        model._meta.db_table
        for model in apps.get_models(include_auto_created=True)
        if model._meta.managed and not model._meta.proxy
    }
    ready = required.issubset(tables)
    _schema_cache.update(ready=ready, checked=now)
    return ready


class ProductionReadinessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/static/"):
            return self.get_response(request)
        try:
            if not schema_ready():
                return HttpResponse(
                    "Production database schema is not ready. Publish the prepared development schema.",
                    status=503, content_type="text/plain",
                )
            if request.path != "/healthz/":
                from accounts.models import User
                from core.models import ProductionBootstrap

                if not User.objects.exists() and not ProductionBootstrap.objects.exists():
                    setup_path = reverse("initialize_production")
                    if request.path != setup_path:
                        return redirect(setup_path)
        except Exception:
            # Do not leak connection strings or signing details to the browser.
            return HttpResponse("Production storage is temporarily unavailable.", status=503,
                                content_type="text/plain")
        return self.get_response(request)
