"""Explicit, additive reference-data setup for an administrator's demo instance."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from core.models import AuditEvent
from org.models import OrgUnit
from providers.models import Provider, ServiceAccount, UtilityType
from workflow.engine import default_policy_steps
from workflow.models import ApprovalPolicy, ApprovalStep


DEMO_PROVIDERS = [
    ("CEB", "Central Electricity Board", UtilityType.ELECTRICITY),
    ("CWA", "Central Water Authority", UtilityType.WATER),
    ("MT", "Mauritius Telecom", UtilityType.TELECOM),
    ("EMTEL", "Emtel Ltd", UtilityType.INTERNET),
    ("WMA", "Wastewater Management Authority", UtilityType.WASTE),
]


def _identity(value):
    return "".join(char for char in value.casefold() if char.isalnum())


def _one(matches, label):
    matches = {item.pk: item for item in matches}
    if len(matches) > 1:
        raise ValidationError(f"More than one existing record matches {label}. Resolve the ambiguity first.")
    return next(iter(matches.values()), None)


@transaction.atomic
def load_demo_reference_data(actor):
    if not actor.is_authenticated or not actor.is_active or not actor.is_superuser:
        raise PermissionDenied
    # Serialise repeat clicks; do not migrate, reset or seed during startup.
    root = OrgUnit.objects.select_for_update().filter(parent__isnull=True).first()
    if root is None:
        raise ValidationError("Complete initial government setup before loading demo reference data.")

    # Reuse the established demo hierarchy and account catalogue, but NEVER
    # invoke the development seed command (users, passwords, banks, payments).
    from core.management.commands.seed_demo import ACCOUNTS, UNITS

    created = {"units": 0, "providers": 0, "accounts": 0, "policies": 0}
    units = {"GOM": root}
    existing_units = list(OrgUnit.objects.all())
    for code, parent_code, kind, name, local in UNITS:
        if parent_code is None:
            continue
        parent = units[parent_code]
        unit = _one([
            item for item in existing_units
            if _identity(item.code) == _identity(code)
            or (item.parent_id == parent.pk and _identity(item.name) == _identity(name))
        ], name)
        if unit and (unit.parent_id != parent.pk or unit.kind != kind):
            raise ValidationError(f"Existing unit {unit.code} has a different hierarchy or type; no data was loaded.")
        if unit is None:
            unit = OrgUnit.objects.create(
                parent=parent, code=code, kind=kind, name=name, name_local=local,
            )
            existing_units.append(unit)
            created["units"] += 1
        units[code] = unit

    providers = {}
    existing_providers = list(Provider.objects.all())
    for code, name, utility in DEMO_PROVIDERS:
        provider = _one([
            item for item in existing_providers
            if _identity(item.short_code) == _identity(code) or _identity(item.name) == _identity(name)
        ], name)
        if provider is None:
            provider = Provider.objects.create(
                name=name, short_code=code, utility_type=utility, currency="MUR",
                identifiers=f"{code}\n{name}", payment_terms_days=30,
            )
            existing_providers.append(provider)
            created["providers"] += 1
        providers[code] = provider

    for unit_code, provider_code, number, premises, _amount, _consumption in ACCOUNTS:
        _, was_created = ServiceAccount.objects.get_or_create(
            provider=providers[provider_code], account_number=f"DEMO-{provider_code}-{number}",
            defaults={
                "org_unit": units[unit_code], "premises": f"Demo: {premises}",
                "service_type": providers[provider_code].get_utility_type_display(),
                "service_details": "Synthetic demonstration service account; not a live utility account.",
                "purpose": "GovBills demonstration only", "tariff": "Demo",
            },
        )
        created["accounts"] += was_created

    if not ApprovalPolicy.objects.filter(org_unit=root).exists():
        policy = ApprovalPolicy.objects.create(name="Standard utility invoice approval", org_unit=root)
        for order, step in enumerate(default_policy_steps(), start=1):
            ApprovalStep.objects.create(policy=policy, order=order, **step)
        created["policies"] += 1

    AuditEvent.record("demo.reference_data_loaded", obj=root, actor=actor, data={
        "created": created, "purpose": "demonstration", "existing_records_overwritten": False,
        "users_or_financial_records_created": False,
    })
    return created
