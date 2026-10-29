"""Local-only acceptance check of the documented seeded demo accounts."""

import hashlib
import io
import json
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from xml.etree import ElementTree

import pyotp
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import OTP_SESSION_KEY
from accounts.models import User
from core.management.commands.seed_demo import DEMO_PASSWORD, DEMO_PIN, DEMO_TOTP, USERS
from invoices.models import Invoice
from invoices.services import save_invoice
from org.models import OrgUnit
from providers.models import ServiceAccount
from settlements import services as settlements
from signatures import services as signatures
from tests.conftest import pdf_bytes
from workflow import engine


@pytest.mark.django_db
def test_seeded_demo_login_permissions_and_signed_payment(settings):
    settings.PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.Argon2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    ]
    # Full demo initialization, but omit historical months for this smoke check.
    # Capture seeder output because it includes a generated demo API token.
    with redirect_stdout(io.StringIO()):
        call_command("seed_demo", months=0, stdout=io.StringIO())

    users = {name: User.objects.get(username=name) for name, *_ in USERS}
    sessions = {}
    results = {"demo_accounts": [], "signing_pins": [], "checks": []}
    totp = pyotp.TOTP(DEMO_TOTP)

    for index, (name, user) in enumerate(users.items(), start=1):
        client = Client(REMOTE_ADDR=f"192.0.2.{index}")
        wrong = client.post(reverse("accounts:login"), {
            "username": name, "password": "intentionally-incorrect-password",
        })
        assert wrong.status_code == 200 and "_auth_user_id" not in client.session
        response = client.post(reverse("accounts:login"), {
            "username": name, "password": DEMO_PASSWORD,
        })
        assert response.status_code == 302
        assert response.url == reverse("accounts:otp_verify")
        assert not client.session.get(OTP_SESSION_KEY)
        protected = client.get(reverse("core:dashboard"))
        assert protected.status_code == 302
        assert reverse("accounts:otp_verify") in protected.url

        valid_codes = {
            totp.at(timezone.now() + timedelta(seconds=offset))
            for offset in (-30, 0, 30)
        }
        invalid_code = next(f"{n:06d}" for n in range(10) if f"{n:06d}" not in valid_codes)
        denied = client.post(reverse("accounts:otp_verify"), {"code": invalid_code})
        assert denied.status_code == 200 and not client.session.get(OTP_SESSION_KEY)
        code = totp.now()
        verified = client.post(reverse("accounts:otp_verify"), {"code": code})
        assert verified.status_code == 302 and client.session.get(OTP_SESSION_KEY)
        assert client.get(reverse("core:dashboard")).status_code == 200
        assert client.get("/invoices/").status_code == 200
        assert client.get("/approvals/").status_code == 200
        assert client.get("/settlements/").status_code == 200
        assert client.get("/analytics/").status_code == 200
        assert client.get("/console/").status_code == (200 if name == "sysadmin" else 403)
        sessions[name] = client
        results["demo_accounts"].append(name)

        if name == "sysadmin":
            replay = Client(REMOTE_ADDR="192.0.2.200")
            replay.post(reverse("accounts:login"), {"username": name, "password": DEMO_PASSWORD})
            replay_response = replay.post(reverse("accounts:otp_verify"), {"code": code})
            assert replay_response.status_code == 200
            assert not replay.session.get(OTP_SESSION_KEY)

    results["checks"].extend([
        "password alone cannot reach dashboard",
        "wrong password rejected",
        "wrong TOTP rejected",
        "correct TOTP grants dashboard access",
        "TOTP replay rejected",
        "admin console restricted to sysadmin",
        "authenticated billing pages render",
    ])
    assert sessions["f.finance"].get("/settlements/new/").status_code == 200
    assert sessions["auditor"].get("/settlements/new/").status_code == 403
    assert sessions["h.clerk"].get("/settlements/new/").status_code == 403
    results["checks"].append("settlement preparation restricted to finance")

    for name in ("m.approver", "k.director", "f.finance", "a.authoriser", "e.approver"):
        with pytest.raises(ValidationError):
            signatures.check_pin(users[name], "intentionally-wrong-pin")
        signatures.check_pin(users[name], DEMO_PIN)
        assert signatures.active_identity(users[name]).failed_pin_attempts == 0
        results["signing_pins"].append(name)

    # Exercise a real high-value invoice through all three approval roles.
    account = ServiceAccount.objects.filter(org_unit__code__startswith="MOHW").first()
    assert account is not None
    today = timezone.localdate()
    number = f"DEMO-ACCEPTANCE-{uuid4().hex[:12]}"
    document = pdf_bytes((number, "Isolated acceptance test invoice"))
    invoice = Invoice(
        service_account=account,
        invoice_number=number,
        issue_date=today,
        due_date=today + timedelta(days=60),
        amount_total=Decimal("600000.00"),
    )
    invoice.document.save(number + ".pdf", SimpleUploadedFile(number + ".pdf", document), save=False)
    invoice.document_sha256 = hashlib.sha256(document).hexdigest()
    invoice = save_invoice(invoice, users["h.clerk"])
    from django.core.files.base import ContentFile
    from reconciliation.services import upload_statement
    import csv
    listing = io.StringIO()
    writer = csv.writer(listing)
    writer.writerow(["account_number", "invoice_number", "amount", "currency"])
    for pending in Invoice.objects.filter(provider=invoice.provider, org_unit=invoice.org_unit,
            currency=invoice.currency, status__in=Invoice.OPEN).select_related("service_account"):
        writer.writerow([pending.service_account.account_number, pending.invoice_number,
                         str(pending.amount_total), pending.currency])
    upload_statement(users["f.finance"], invoice.provider, invoice.org_unit, invoice.currency, today,
                     ContentFile(listing.getvalue().encode(), name="acceptance-provider-pending.csv"))
    engine.submit(invoice, users["h.clerk"])
    pending = invoice.approval_tasks.get(status="pending")
    with pytest.raises(PermissionDenied):
        engine.decide(pending, users["h.clerk"], engine.Decision.APPROVE)
    decisions = []
    for name in ("s.verifier", "m.approver", "k.director"):
        pending = invoice.approval_tasks.get(status="pending")
        task = engine.decide(
            pending, users[name], engine.Decision.APPROVE,
            comment="Isolated demo acceptance check; high value and any flagged variance are intentional.",
            pin=DEMO_PIN,
        )
        decisions.append(name)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.APPROVED
    assert invoice.approval_tasks.filter(status="approved").count() == 3
    for task in invoice.approval_tasks.exclude(signature__isnull=True):
        valid, findings = signatures.verify_signature(task.signature)
        assert valid, findings
    results["checks"].append("clerk cannot approve own invoice")
    results["checks"].append("verifier, approver and director complete high-value approval")
    results["checks"].append("approval signatures verify")
    results["approval_actors"] = decisions

    ministry = OrgUnit.objects.get(code="MOHW")
    with pytest.raises(PermissionDenied):
        settlements.create_instruction(
            users["h.clerk"], ministry, "MUR", today + timedelta(days=90), [invoice.pk]
        )
    instruction = settlements.create_instruction(
        users["f.finance"], ministry, "MUR", today + timedelta(days=90), [invoice.pk]
    )
    assert instruction.total_amount == Decimal("600000.00")
    settlements.submit_for_authorisation(instruction, users["f.finance"], DEMO_PIN)
    with pytest.raises(PermissionDenied):
        settlements.authorise(instruction, users["f.finance"], DEMO_PIN)
    instruction = settlements.authorise(instruction, users["a.authoriser"], DEMO_PIN)
    with instruction.pdf.open("rb") as signed_pdf:
        reports = signatures.validate_pdf(signed_pdf.read())
    assert [report["field"] for report in reports] == ["Prepared", "Authorised"]
    assert all(report["ok"] and report["intact"] and report["trusted"] for report in reports)
    bank_file = ElementTree.fromstring(settlements.pain001(instruction))
    namespace = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
    assert bank_file.find(".//p:GrpHdr/p:CtrlSum", namespace).text == "600000.00"
    settlements.mark_executed(instruction, users["f.finance"], "ISOLATED-DEMO-TEST", today)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
    assert invoice.payment_reference == "ISOLATED-DEMO-TEST"
    results["checks"].extend([
        "finance prepares and signs settlement",
        "preparer cannot authorise own settlement",
        "authoriser countersigns settlement",
        "both settlement PDF signatures validate",
        "ISO 20022 export total matches invoice",
        "recording execution marks test invoice paid",
    ])
    print("DEMO_ACCEPTANCE_RESULT=" + json.dumps(results, sort_keys=True))
