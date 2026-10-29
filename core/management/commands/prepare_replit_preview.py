"""Initialize only the workspace's private, disposable demo instance."""

import io
from contextlib import redirect_stdout

from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command


class Command(BaseCommand):
    help = "Prepare the Replit workspace preview without changing production."

    def handle(self, *args, **options):
        if settings.PRODUCTION or not getattr(settings, "REPLIT_PREVIEW", False):
            raise CommandError("Demo initialization is allowed only in the Replit development preview.")
        if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("Preview initialization requires its isolated local SQLite database.")
        call_command("migrate", interactive=False, verbosity=0)
        from accounts.models import User

        if not User.objects.exists():
            # The original command prints a generated integration token.
            # Never write those credentials into workflow logs.
            with redirect_stdout(io.StringIO()):
                call_command("seed_demo", months=0, stdout=io.StringIO(), stderr=io.StringIO())
            self.stdout.write("Initialized isolated GovBills demo accounts and sample invoices.")
        else:
            self.stdout.write("Using existing GovBills workspace data; no accounts were reset.")
        call_command("collectstatic", interactive=False, verbosity=0)
