import hashlib
import json

from django.conf import settings
from core.work_models import ProcessingCompletion, ProcessingSchedule  # noqa: F401
from core.space_models import SpaceFolder  # noqa: F401
from django.core.exceptions import PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.utils import timezone

GENESIS_HASH = "0" * 64


class ProductionBootstrap(models.Model):
    """Permanent one-time setup marker, even if staff accounts are later removed."""

    singleton = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    completed_at = models.DateTimeField(auto_now_add=True)


def canonical_json(data):
    """Deterministic JSON used for hashing and signing."""
    return json.dumps(data, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AuditHead(models.Model):
    """Single row holding the tip of the audit chain; locked while appending."""

    last_sequence = models.BigIntegerField(default=0)
    last_hash = models.CharField(max_length=64, default=GENESIS_HASH)


class AuditEvent(models.Model):
    """
    Append-only, hash-chained audit trail. Each entry commits to the one
    before it, so removing or altering any row breaks `verify_chain`.
    """

    sequence = models.BigIntegerField(unique=True)
    occurred_at = models.DateTimeField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="+")
    actor_label = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=64, db_index=True)
    object_type = models.CharField(max_length=64, blank=True, db_index=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    org_unit_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    data = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    prev_hash = models.CharField(max_length=64)
    entry_hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["-sequence"]

    def hash_payload(self):
        return canonical_json({
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(),
            "actor": self.actor_id,
            "actor_label": self.actor_label,
            "action": self.action,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "org_unit_id": self.org_unit_id,
            "ip_address": self.ip_address,
            "data": self.data,
            "prev_hash": self.prev_hash,
        })

    def compute_hash(self):
        return hashlib.sha256(self.hash_payload().encode()).hexdigest()

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionDenied("Audit events are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Audit events are immutable")

    @classmethod
    def record(cls, action, obj=None, actor=None, data=None, org_unit_id=None, ip_address=None):
        from core.request_context import current_ip, current_user

        actor = actor if actor is not None else current_user()
        if actor is not None and not getattr(actor, "is_authenticated", False):
            actor = None
        if org_unit_id is None and obj is not None:
            org_unit_id = getattr(obj, "org_unit_id", None)
        # Round-trip through JSON so the stored value hashes identically later.
        clean = json.loads(canonical_json(data or {}))
        with transaction.atomic():
            head, _ = AuditHead.objects.select_for_update().get_or_create(pk=1)
            event = cls(
                sequence=head.last_sequence + 1,
                occurred_at=timezone.now().replace(microsecond=0),
                actor=actor,
                actor_label=actor.get_username() if actor else "system",
                action=action,
                object_type=obj._meta.label_lower if obj is not None else "",
                object_id=str(obj.pk) if obj is not None else "",
                org_unit_id=org_unit_id,
                ip_address=ip_address or current_ip(),
                data=clean,
                prev_hash=head.last_hash,
            )
            event.entry_hash = event.compute_hash()
            event.save()
            head.last_sequence = event.sequence
            head.last_hash = event.entry_hash
            head.save(update_fields=["last_sequence", "last_hash"])
            # Persist the role-scoped alert in the same transaction as the
            # activity; external transports run only after successful commit.
            from core.action_notifications import dispatch
            dispatch(event, obj)
        return event

    @classmethod
    def verify_chain(cls):
        """Return (ok, first_bad_sequence, checked_count)."""
        expected_prev = GENESIS_HASH
        expected_seq = 1
        count = 0
        for event in cls.objects.order_by("sequence").iterator():
            if event.sequence != expected_seq or event.prev_hash != expected_prev \
                    or event.compute_hash() != event.entry_hash:
                return False, event.sequence, count
            expected_prev = event.entry_hash
            expected_seq += 1
            count += 1
        head = AuditHead.objects.filter(pk=1).first()
        if head and (head.last_sequence != count or head.last_hash != expected_prev):
            return False, count + 1, count
        return True, None, count


class Holiday(models.Model):
    """Public holidays excluded from SLA working-time calculations."""

    date = models.DateField(unique=True)
    name = models.CharField(max_length=120)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} {self.name}"


class Notification(models.Model):
    class Level(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=300, blank=True)
    # Prevents the scheduler from repeating the same alert.
    dedupe_key = models.CharField(max_length=200, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "dedupe_key"], condition=~models.Q(dedupe_key=""),
                                    name="unique_notification_dedupe"),
        ]


class Counter(models.Model):
    """Gap-free document numbering, e.g. INV-2026-000042."""

    key = models.CharField(max_length=40, unique=True)
    value = models.BigIntegerField(default=0)

    @classmethod
    def next(cls, prefix, width=6):
        year = timezone.localdate().year
        key = f"{prefix}-{year}"
        with transaction.atomic():
            counter, _ = cls.objects.select_for_update().get_or_create(key=key)
            counter.value += 1
            counter.save(update_fields=["value"])
        return f"{key}-{counter.value:0{width}d}"
