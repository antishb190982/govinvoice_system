from django.core.management.base import BaseCommand, CommandError

from core.models import AuditEvent


class Command(BaseCommand):
    help = "Recompute the audit hash chain and fail if any entry was altered or removed."

    def handle(self, *args, **options):
        ok, bad, count = AuditEvent.verify_chain()
        if not ok:
            raise CommandError(f"Audit chain broken at sequence {bad} (checked {count} entries).")
        self.stdout.write(self.style.SUCCESS(f"Audit chain intact: {count} entries."))
