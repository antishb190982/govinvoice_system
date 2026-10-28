import io
from datetime import timedelta
from decimal import Decimal

import pyotp
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from reportlab.pdfgen import canvas

from accounts.models import User
from org.models import Membership, OrgUnit, Role
from providers.models import Provider, ServiceAccount, UtilityType
from workflow.models import ApprovalPolicy, ApprovalStep

PASSWORD = "Test-Passw0rd-123!"
PIN = "Pin-Code-2026"


@pytest.fixture(autouse=True)
def isolated_storage(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "private"
    settings.SIGNING_CA_DIR = tmp_path / "ca"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    # Every day is a working day (00:00-23:00) so SLA clocks always run in tests.
    settings.WORK_WEEK_DAYS = [0, 1, 2, 3, 4, 5, 6]
    settings.WORK_DAY_START_HOUR = 0
    settings.WORK_DAY_END_HOUR = 23
    settings.SETTLEMENT_LEAD_DAYS = 3
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def pdf_bytes(text_lines=("Test invoice",)):
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in text_lines:
        c.drawString(50, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


def upload_pdf(name="invoice.pdf", lines=("Test invoice",)):
    return SimpleUploadedFile(name, pdf_bytes(lines), content_type="application/pdf")


@pytest.fixture
def tree(db):
    gov = OrgUnit.objects.create(kind="government", code="GOV", name="Government")
    moh = OrgUnit.objects.create(kind="ministry", code="MOH", name="Health", parent=gov)
    hosp = OrgUnit.objects.create(kind="site", code="MOH-H1", name="Hospital", parent=moh)
    moe = OrgUnit.objects.create(kind="ministry", code="MOE", name="Education", parent=gov)
    return {"gov": gov, "moh": moh, "hosp": hosp, "moe": moe}


def make_user(username, roles=(), superuser=False):
    user = User.objects.create(username=username, full_name=username.title(), email=f"{username}@example.gov",
                               is_superuser=superuser, totp_secret=pyotp.random_base32(),
                               totp_confirmed_at=timezone.now())
    user.set_password(PASSWORD)
    user.save()
    for role, unit in roles:
        Membership.objects.create(user=user, org_unit=unit, role=role)
    return user


@pytest.fixture
def people(tree):
    return {
        "clerk": make_user("clerk", [(Role.CLERK, tree["moh"])]),
        "verifier": make_user("verifier", [(Role.VERIFIER, tree["moh"])]),
        "approver": make_user("approver", [(Role.APPROVER, tree["moh"])]),
        "approver2": make_user("approver2", [(Role.APPROVER, tree["moh"])]),
        "admin": make_user("admin", [(Role.UNIT_ADMIN, tree["gov"])]),
        "finance": make_user("finance", [(Role.FINANCE, tree["moh"])]),
        "authoriser": make_user("authoriser", [(Role.AUTHORISER, tree["moh"])]),
        "outsider": make_user("outsider", [(Role.CLERK, tree["moe"]), (Role.APPROVER, tree["moe"])]),
    }


@pytest.fixture
def provider(db):
    return Provider.objects.create(name="Power Co", short_code="PWR", utility_type=UtilityType.ELECTRICITY,
                                   bank_iban="OM810180000001299123456", bank_account_name="Power Co Collections",
                                   bank_bic="BMUSOMRX", currency="OMR")


@pytest.fixture
def account(tree, provider):
    return ServiceAccount.objects.create(provider=provider, org_unit=tree["hosp"], account_number="ACC-1",
                                         premises="Main building")


@pytest.fixture
def policy(tree):
    p = ApprovalPolicy.objects.create(name="Default", org_unit=tree["gov"])
    ApprovalStep.objects.create(policy=p, order=1, name="Verify", role=Role.VERIFIER, sla_hours=8)
    ApprovalStep.objects.create(policy=p, order=2, name="Approve", role=Role.APPROVER, sla_hours=16,
                                requires_signature=True)
    ApprovalStep.objects.create(policy=p, order=3, name="Senior", role=Role.APPROVER, sla_hours=16,
                                requires_signature=True, min_amount=Decimal("5000"))
    return p


@pytest.fixture
def make_invoice(account, people):
    from invoices.models import Invoice
    from invoices.services import save_invoice

    counter = {"n": 0}

    def _make(amount="1000.000", due_in=30, number=None, **extra):
        counter["n"] += 1
        today = timezone.localdate()
        inv = Invoice(service_account=account, invoice_number=number or f"INV-{counter['n']}",
                      issue_date=today - timedelta(days=2), due_date=today + timedelta(days=due_in),
                      amount_total=Decimal(amount), **extra)
        f = upload_pdf(f"inv{counter['n']}.pdf", (f"Invoice {counter['n']}",))
        inv.document.save(f.name, f, save=False)
        import hashlib

        f.seek(0)
        inv.document_sha256 = hashlib.sha256(f.read()).hexdigest()
        return save_invoice(inv, people["clerk"])

    return _make


@pytest.fixture
def signers(people):
    from signatures.services import enroll

    for key in ("approver", "approver2", "finance", "authoriser"):
        enroll(people[key], PIN)
    return people
