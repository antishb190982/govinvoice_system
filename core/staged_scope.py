"""Scope uncommitted files using their validated account allocation, not raw labels."""
from django.db.models import CharField, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from providers.models import ServiceAccount


def staged_unit(qs, unit, *, batches=False):
    row = "rows__" if batches else ""
    batch = "" if batches else "batch__"
    account_ids = ServiceAccount.objects.filter(org_unit__path__startswith=unit.path).annotate(
        scope_key=Cast("pk", CharField(max_length=32))).values("scope_key")
    return qs.alias(staged_account_key=Cast(
        KeyTextTransform("service_account_id", row + "data"), CharField(max_length=32))).filter(
            Q(**{batch + "preparations__org_unit__path__startswith": unit.path}) |
            Q(**{row + "invoice__org_unit__path__startswith": unit.path}) |
            Q(staged_account_key__in=account_ids)).distinct()


def staged_provider(qs, provider, *, batches=False):
    row = "rows__" if batches else ""
    batch = "" if batches else "batch__"
    return qs.filter(
        Q(**{batch + "provider": provider}) |
        Q(**{batch + "preparations__provider": provider}) |
        Q(**{row + "invoice__provider": provider}) |
        Q(**{row + "data__provider_id": provider.pk})).distinct()
