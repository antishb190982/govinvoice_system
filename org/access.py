"""
Row-level access control. A membership grants its role on the unit and every
unit below it; nothing grants access sideways or upwards. Superusers (system
administrators) see everything.
"""

from django.db.models import Q
from django.utils import timezone

from org.models import Membership, OrgUnit, Role

READ_ROLES = set(Role.values)


def active_memberships(user, roles=None):
    if not user.is_authenticated:
        return Membership.objects.none()
    today = timezone.localdate()
    qs = Membership.objects.filter(user=user, user__is_active=True).filter(
        Q(valid_until__isnull=True) | Q(valid_until__gte=today)
    ).select_related("org_unit")
    if roles:
        qs = qs.filter(role__in=list(roles))
    return qs


def unit_filter(user, roles=None, prefix="org_unit__"):
    """A Q object restricting `<prefix>path` to the user's subtrees."""
    if user.is_superuser:
        return Q()
    paths = {m.org_unit.path for m in active_memberships(user, roles)}
    if not paths:
        return Q(pk__in=[])
    q = Q()
    for path in paths:
        q |= Q(**{f"{prefix}path__startswith": path})
    return q


def units_for(user, roles=None):
    if user.is_superuser:
        return OrgUnit.objects.all()
    return OrgUnit.objects.filter(unit_filter(user, roles, prefix=""))


def scope(queryset, user, roles=None, prefix="org_unit__"):
    return queryset.filter(unit_filter(user, roles, prefix)).distinct()


def has_role(user, org_unit, roles):
    """True if the user holds any of `roles` on org_unit or one of its ancestors."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if isinstance(roles, str):
        roles = [roles]
    return active_memberships(user, roles).filter(org_unit_id__in=org_unit.ancestor_ids).exists()


def can_view(user, org_unit):
    return has_role(user, org_unit, READ_ROLES)


def users_with_role(org_unit, roles, include_ancestors=True):
    from django.contrib.auth import get_user_model

    if isinstance(roles, str):
        roles = [roles]
    unit_ids = org_unit.ancestor_ids if include_ancestors else [org_unit.pk]
    today = timezone.localdate()
    return get_user_model().objects.filter(
        is_active=True,
        memberships__org_unit_id__in=unit_ids,
        memberships__role__in=roles,
    ).filter(Q(memberships__valid_until__isnull=True) | Q(memberships__valid_until__gte=today)).distinct()


def roles_on(user, org_unit):
    if user.is_superuser:
        return set(Role.values)
    return set(active_memberships(user).filter(org_unit_id__in=org_unit.ancestor_ids)
               .values_list("role", flat=True))


def user_has_any_role(user, roles):
    return user.is_superuser or active_memberships(user, roles).exists()
