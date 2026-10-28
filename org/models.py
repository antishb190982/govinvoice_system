from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction


class OrgUnit(models.Model):
    """
    A node of the government structure: government → ministry → directorate
    → department → site. `path` ("/1/4/9/") makes subtree queries a single
    prefix match.
    """

    class Kind(models.TextChoices):
        GOVERNMENT = "government", "Government"
        MINISTRY = "ministry", "Ministry"
        AUTHORITY = "authority", "Authority / Agency"
        DIRECTORATE = "directorate", "Directorate"
        DEPARTMENT = "department", "Department"
        SITE = "site", "Site / Facility"

    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    code = models.CharField(max_length=32, unique=True, help_text="Budget / cost-centre code")
    name = models.CharField(max_length=200)
    name_local = models.CharField(max_length=200, blank=True, help_text="Name in the local language")
    budget_reference = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    path = models.CharField(max_length=512, editable=False, db_index=True, default="")
    depth = models.PositiveSmallIntegerField(editable=False, default=0)

    class Meta:
        ordering = ["path"]

    def __str__(self):
        return f"{self.code} · {self.name}"

    def clean(self):
        if self.parent_id and self.pk and f"/{self.pk}/" in self.parent.path:
            raise ValidationError({"parent": "A unit cannot be moved below itself."})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            old_path = self.path
            super().save(*args, **kwargs)
            prefix = self.parent.path if self.parent_id else "/"
            new_path = f"{prefix}{self.pk}/"
            if new_path != old_path:
                self.depth = new_path.count("/") - 2
                OrgUnit.objects.filter(pk=self.pk).update(path=new_path, depth=self.depth)
                if old_path:
                    for child in OrgUnit.objects.filter(path__startswith=old_path).exclude(pk=self.pk):
                        child.path = new_path + child.path[len(old_path):]
                        child.depth = child.path.count("/") - 2
                        OrgUnit.objects.filter(pk=child.pk).update(path=child.path, depth=child.depth)
                self.path = new_path

    @property
    def ancestor_ids(self):
        return [int(p) for p in self.path.strip("/").split("/") if p]

    def ancestors(self, include_self=True):
        ids = self.ancestor_ids if include_self else self.ancestor_ids[:-1]
        return OrgUnit.objects.filter(pk__in=ids).order_by("depth")

    def descendants(self, include_self=True):
        qs = OrgUnit.objects.filter(path__startswith=self.path)
        return qs if include_self else qs.exclude(pk=self.pk)

    def ministry(self):
        """Nearest ancestor that is a ministry or authority (or the top unit)."""
        chain = list(self.ancestors())
        for unit in reversed(chain):
            if unit.kind in (self.Kind.MINISTRY, self.Kind.AUTHORITY):
                return unit
        return chain[0] if chain else self

    @property
    def indented_name(self):
        return " " * self.depth + self.name


class Role(models.TextChoices):
    VIEWER = "viewer", "Viewer"
    CLERK = "clerk", "Invoice clerk"
    VERIFIER = "verifier", "Verifier"
    APPROVER = "approver", "Approver"
    FINANCE = "finance", "Finance officer"
    AUTHORISER = "authoriser", "Payment authoriser"
    UNIT_ADMIN = "unit_admin", "Unit administrator"
    AUDITOR = "auditor", "Auditor"


class Membership(models.Model):
    """A role held by a user on a unit; it applies to the unit and all units below it."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    org_unit = models.ForeignKey(OrgUnit, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices)
    valid_until = models.DateField(null=True, blank=True, help_text="Optional expiry, e.g. for acting delegations")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "org_unit", "role"], name="unique_membership")]
        ordering = ["org_unit__path", "role"]

    def __str__(self):
        return f"{self.user} – {self.get_role_display()} @ {self.org_unit.code}"
