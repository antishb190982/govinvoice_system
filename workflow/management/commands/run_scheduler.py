import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from invoices.services import mark_overdue_flags
from settlements.services import sweep_due
from workflow.engine import sweep_sla


class Command(BaseCommand):
    help = "SLA reminders/escalations, due-date alerts and overdue flags. Run every few minutes."

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, default=0, help="Repeat every N seconds (for a worker container)")

    def handle(self, *args, loop=0, **options):
        while True:
            close_old_connections()
            reminded, escalated = sweep_sla()
            alerts = sweep_due()
            overdue = mark_overdue_flags()
            self.stdout.write(f"reminders={reminded} escalations={escalated} due_alerts={alerts} overdue={overdue}")
            if not loop:
                break
            time.sleep(loop)
