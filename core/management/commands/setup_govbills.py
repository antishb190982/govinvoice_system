from django.core.management.base import BaseCommand
from django.db import transaction

from org.models import OrgUnit
from signatures.ca import ensure_ca
from workflow.engine import default_policy_steps
from workflow.models import ApprovalPolicy, ApprovalStep


class Command(BaseCommand):
    help = "First-run setup: signing CA, top-level government unit, default approval policy."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Government")
        parser.add_argument("--code", default="GOV")

    @transaction.atomic
    def handle(self, *args, name, code, **options):
        ensure_ca()
        root = OrgUnit.objects.filter(parent__isnull=True).first()
        if root is None:
            root = OrgUnit.objects.create(kind=OrgUnit.Kind.GOVERNMENT, code=code, name=name)
            self.stdout.write(f"Created top-level unit {root}")
        if not ApprovalPolicy.objects.filter(org_unit=root).exists():
            policy = ApprovalPolicy.objects.create(name="Standard utility invoice approval", org_unit=root)
            for i, step in enumerate(default_policy_steps(), start=1):
                ApprovalStep.objects.create(policy=policy, order=i, **step)
            self.stdout.write(f"Created default policy “{policy.name}”")
        self.stdout.write(self.style.SUCCESS("Setup complete. Create a system administrator with createsuperuser."))
