"""Small, isolated demo that exercises the real approval/settlement services."""

import hashlib
import io
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from accounts.models import User
from core.management.commands.seed_demo import DEMO_PASSWORD, DEMO_PIN, DEMO_TOTP
from core.models import AuditEvent
from invoices.models import Invoice
from invoices.services import save_invoice
from operations import services as batches
from org.models import Membership, OrgUnit, Role
from providers.models import Provider, ServiceAccount
from reconciliation.services import upload_statement
from signatures import services as signing
from signatures.appearance import normalise_image
from workflow.batch_approval import decide_batch, stage_tasks
from workflow.engine import Decision
from workflow.models import ApprovalPolicy, ApprovalStep


def demo_image(role):
    image = Image.new("RGB", (450, 130), "white")
    draw = ImageDraw.Draw(image)
    draw.line([(20, 65), (60, 25), (45, 90), (115, 40), (145, 75),
               (190, 35), (225, 60), (310, 45), (400, 65)], fill="#204a91", width=4)
    draw.text((20, 105), f"DEMO SIGNATURE ONLY - {role.upper()}", fill="#204a91")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class Command(BaseCommand):
    help = "Populate an isolated CEB-provider demonstration; never creates a genuine CEB-issued bill."

    def add_arguments(self, parser):
        parser.add_argument("--scenario", choices=["sample", "journey"], default="sample")
        parser.add_argument("--complete", action="store_true")

    def handle(self, *args, **options):
        if settings.PRODUCTION:
            raise CommandError("Demo population is disabled in production.")
        provider = Provider.objects.get(short_code="CEB")
        root = OrgUnit.objects.filter(kind=OrgUnit.Kind.GOVERNMENT).first()
        unit, _ = OrgUnit.objects.get_or_create(code="STAMP-DEMO", defaults={
            "kind": OrgUnit.Kind.MINISTRY, "name": "DEMO - Approval stamp testing", "parent": root})
        users = {}
        for key, role in [("clerk", Role.CLERK), ("verifier", Role.VERIFIER), ("approver", Role.APPROVER),
                          ("finance", Role.FINANCE), ("authoriser", Role.AUTHORISER)]:
            user, created = User.objects.get_or_create(username=f"stamp.{key}", defaults={
                "full_name": f"Demo Stamp {key.title()}", "email": f"stamp.{key}@example.invalid",
                "totp_secret": DEMO_TOTP, "totp_confirmed_at": timezone.now()})
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
            Membership.objects.get_or_create(user=user, org_unit=unit, role=role)
            users[key] = user
            if role != Role.CLERK and not signing.active_identity(user):
                signing.enroll(user, DEMO_PIN)
            if role in [Role.VERIFIER, Role.APPROVER]:
                identity = signing.active_identity(user)
                if not identity.signature_image:
                    data, digest = normalise_image(SimpleUploadedFile("demo.png", demo_image(key)))
                    identity.signature_image.save("demo.png", ContentFile(data), save=False)
                    identity.signature_image_sha256 = digest
                    identity.save(update_fields=["signature_image", "signature_image_sha256"])
                    AuditEvent.record("signature.appearance_changed", obj=identity, actor=user,
                                      data={"sha256": digest, "demonstration": True})
        policy, _ = ApprovalPolicy.objects.get_or_create(org_unit=unit, name="DEMO - Stamp review and approval")
        for order, role in [(1, Role.VERIFIER), (2, Role.APPROVER)]:
            ApprovalStep.objects.get_or_create(policy=policy, order=order, defaults={
                "name": f"Demo {role.label}", "role": role, "requires_signature": True, "sla_hours": 24})
        account, _ = ServiceAccount.objects.get_or_create(provider=provider, account_number="DEMO-CEB-STAMP-001",
            defaults={"org_unit": unit, "premises": "Demonstration premises - not a real service account"})
        number = f"DEMO-CEB-STAMP-{options['scenario'].upper()}"
        invoice = Invoice.objects.filter(invoice_number=number, service_account=account).first()
        if not invoice:
            today = timezone.localdate()
            output = io.BytesIO()
            pdf = canvas.Canvas(output, pagesize=A4)
            pdf.setFillColorRGB(.7, .12, .12)
            pdf.setFont("Helvetica-Bold", 12)
            pdf.drawString(40, 797, "DEMONSTRATION - NOT A CEB-ISSUED BILL")
            pdf.setFillColorRGB(.1, .18, .3)
            pdf.setFont("Helvetica-Bold", 20)
            pdf.drawString(40, 750, "Electricity invoice - CEB provider demo")
            pdf.setFont("Helvetica", 11)
            lines = [
                f"Invoice: {number}", f"Account: {account.account_number}",
                "Ministry: DEMO - Approval stamp testing", f"Issued / received: {today}",
                f"Due: {today + timedelta(days=20)}", "Service: Demonstration electricity supply",
                "Consumption: 130 kWh (fictional)", "Net charge: MUR 1,000.00",
                "Tax: MUR 150.00 (demonstration value)", "Total payable: MUR 1,150.00",
                "No real customer, service account, debt or bank transfer is represented.",
            ]
            for index, line in enumerate(lines):
                pdf.drawString(40, 700 - index * 28, line)
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(40, 130, "DEMO DOCUMENT - Replace with an uploaded genuine CEB invoice.")
            pdf.save()
            source = output.getvalue()
            invoice = Invoice(service_account=account, invoice_number=number, issue_date=today,
                received_date=today, due_date=today + timedelta(days=20),
                period_start=today - timedelta(days=30), period_end=today - timedelta(days=1),
                amount_net=Decimal("1000.000"), amount_tax=Decimal("150.000"), amount_total=Decimal("1150.000"))
            invoice.document.save(f"{number}.pdf", ContentFile(source), save=False)
            invoice.document_name = f"{number}.pdf"
            invoice.document_sha256 = hashlib.sha256(source).hexdigest()
            save_invoice(invoice, users["clerk"])
            batch = batches.create_batch(users["clerk"], unit, f"DEMO - CEB approval stamp {options['scenario']}",
                                         [invoice.pk])
        else:
            batch = invoice.batch_memberships.get(active=True).batch
        active_invoices = Invoice.objects.filter(service_account=account).exclude(status__in=["paid", "cancelled"])
        csv = "account_number,invoice_number,amount,currency\r\n" + "".join(
            f"{account.account_number},{item.invoice_number},{item.amount_total},MUR\r\n" for item in active_invoices)
        upload_statement(users["finance"], provider, unit, "MUR", timezone.localdate(),
                         SimpleUploadedFile("DEMO-provider-statement.csv", csv.encode(), content_type="text/csv"))
        if options["complete"] and invoice.status != Invoice.Status.PAID:
            if not batch.started_at:
                batches.start_batch(batch, users["clerk"])
            invoice.refresh_from_db()
            while invoice.status == Invoice.Status.IN_APPROVAL:
                task = invoice.approval_tasks.get(status="pending")
                actor = users["verifier"] if task.role == Role.VERIFIER else users["approver"]
                tasks = stage_tasks(batch, actor)
                decide_batch(batch, actor, Decision.APPROVE, "DEMO ONLY: fictional invoice validated for stamp testing.",
                             DEMO_PIN, [task.pk for task in tasks])
                invoice.refresh_from_db()
            from settlements import services as settlements
            instruction = settlements.create_instruction(users["finance"], unit, "MUR",
                timezone.localdate() + timedelta(days=30), [invoice.pk],
                notes="DEMO ONLY - no live transfer", batch_ids=[batch.pk])
            settlements.submit_for_authorisation(instruction, users["finance"], DEMO_PIN)
            settlements.authorise(instruction, users["authoriser"], DEMO_PIN)
            settlements.mark_executed(instruction, users["finance"],
                f"DEMO-NO-TRANSFER-{batch.reference}", timezone.localdate())
            invoice.refresh_from_db()
        self.stdout.write(f"Demo invoice={invoice.pk} {invoice.reference} status={invoice.status}; batch={batch.pk} {batch.reference}")
