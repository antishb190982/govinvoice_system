"""
Demo data for evaluation: a small government with three ministries, five
utility providers, 18 months of seasonal invoice history and live work in
every workflow state. Development only.
"""

import io
import math
import random
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from accounts.models import RecoveryCode, User
from core.models import Holiday
from invoices.models import Invoice
from invoices.services import save_invoice
from org.models import Membership, OrgUnit, Role
from providers.models import Provider, ServiceAccount, UtilityType
from signatures.services import enroll
from workflow import engine
from workflow.models import ApprovalTask

DEMO_PASSWORD = "Demo-Passw0rd-2026!"
DEMO_PIN = "Sign-2026!"
DEMO_TOTP = "JBSWY3DPEHPK3PXP"  # shared demo secret; real users enrol their own

UNITS = [
    ("GOV", None, "government", "Government of the Sultanate (demo)", ""),
    ("MOH", "GOV", "ministry", "Ministry of Health", "وزارة الصحة"),
    ("MOH-HOS", "MOH", "directorate", "Directorate General of Hospitals", ""),
    ("MOH-HOS-RH", "MOH-HOS", "site", "Central Referral Hospital", ""),
    ("MOH-HOS-NH", "MOH-HOS", "site", "North District Hospital", ""),
    ("MOH-PHC", "MOH", "directorate", "Primary Health Care", ""),
    ("MOE", "GOV", "ministry", "Ministry of Education", "وزارة التربية والتعليم"),
    ("MOE-MCT", "MOE", "directorate", "Capital Region Schools", ""),
    ("MOE-DHO", "MOE", "directorate", "Southern Region Schools", ""),
    ("MOI", "GOV", "ministry", "Ministry of Interior", "وزارة الداخلية"),
    ("MOI-HQ", "MOI", "department", "Headquarters Facilities", ""),
]

PROVIDERS = [
    ("National Electricity Distribution Co.", "NEDC", UtilityType.ELECTRICITY, "OM810180000001299123456", "NEDC Collections", "BMUSOMRX"),
    ("Capital Water & Wastewater Services", "CWWS", UtilityType.WATER, "OM280270000002010000789", "CWWS Revenue Account", "NBOMOMRX"),
    ("TeleNet Communications", "TNC", UtilityType.TELECOM, "OM590130000006543210987", "TeleNet Receivables", "BDOFOMRX"),
    ("FibreLink Data", "FLD", UtilityType.INTERNET, "OM830180000001288877766", "FibreLink Billing", "BMUSOMRX"),
    ("Metro Waste Management", "MWM", UtilityType.WASTE, "OM170330000004455667788", "MWM Collections", "OABBOMRX"),
]

# unit code, provider code, account, premises, base monthly amount, base consumption
ACCOUNTS = [
    ("MOH-HOS-RH", "NEDC", "100200301", "Main tower", 18500, 410000),
    ("MOH-HOS-RH", "NEDC", "100200302", "Outpatient block", 6200, 138000),
    ("MOH-HOS-RH", "CWWS", "W-778201", "Hospital campus", 3900, 21000),
    ("MOH-HOS-RH", "TNC", "TN-55001", "Switchboard & mobiles", 1450, 52000),
    ("MOH-HOS-RH", "FLD", "FL-90011", "Campus fibre 1 Gbps", 980, 42000),
    ("MOH-HOS-RH", "MWM", "MW-3301", "Clinical & general waste", 2100, 38),
    ("MOH-HOS-NH", "NEDC", "100355870", "Hospital building", 7600, 168000),
    ("MOH-HOS-NH", "CWWS", "W-779455", "Hospital building", 1650, 8800),
    ("MOH-HOS-NH", "TNC", "TN-55102", "Telephony", 520, 18000),
    ("MOH-PHC", "NEDC", "100410012", "Health centres cluster A", 3100, 69000),
    ("MOH-PHC", "NEDC", "100410013", "Health centres cluster B", 2800, 62000),
    ("MOH-PHC", "CWWS", "W-780120", "Health centres", 640, 3400),
    ("MOH-PHC", "FLD", "FL-90200", "Clinic connectivity", 760, 15000),
    ("MOE-MCT", "NEDC", "200110450", "Schools – Capital zone 1", 9800, 215000),
    ("MOE-MCT", "NEDC", "200110451", "Schools – Capital zone 2", 8700, 192000),
    ("MOE-MCT", "CWWS", "W-801001", "Schools – Capital", 2300, 12500),
    ("MOE-MCT", "FLD", "FL-91000", "Schools broadband", 1900, 88000),
    ("MOE-DHO", "NEDC", "200540010", "Schools – Southern", 7100, 158000),
    ("MOE-DHO", "CWWS", "W-805300", "Schools – Southern", 1200, 6700),
    ("MOE-DHO", "MWM", "MW-4410", "Schools waste", 650, 12),
    ("MOI-HQ", "NEDC", "300900001", "HQ complex", 11200, 247000),
    ("MOI-HQ", "CWWS", "W-900100", "HQ complex", 1400, 7600),
    ("MOI-HQ", "TNC", "TN-60001", "HQ telephony & mobiles", 2600, 91000),
    ("MOI-HQ", "FLD", "FL-95000", "HQ data centre links", 2200, 120000),
]

USERS = [
    # username, full name, title, [(role, unit)]
    ("sysadmin", "System Administrator", "ICT Security", [], True),
    ("h.clerk", "Huda Al-Balushi", "Accounts Clerk", [(Role.CLERK, "MOH")], False),
    ("s.verifier", "Salim Al-Harthy", "Facilities Engineer", [(Role.VERIFIER, "MOH")], False),
    ("m.approver", "Maryam Al-Lawati", "Budget Holder, Hospitals", [(Role.APPROVER, "MOH")], False),
    ("k.director", "Khalid Al-Rawahi", "Director of Finance", [(Role.APPROVER, "MOH"), (Role.UNIT_ADMIN, "MOH")], False),
    ("f.finance", "Fatma Al-Siyabi", "Finance Officer", [(Role.FINANCE, "MOH"), (Role.FINANCE, "MOE")], False),
    ("a.authoriser", "Ahmed Al-Kindi", "Payments Authoriser", [(Role.AUTHORISER, "MOH"), (Role.AUTHORISER, "MOE")], False),
    ("e.clerk", "Aisha Al-Maskari", "Accounts Clerk", [(Role.CLERK, "MOE"), (Role.CLERK, "MOI")], False),
    ("e.verifier", "Yousuf Al-Hinai", "Facilities Officer", [(Role.VERIFIER, "MOE"), (Role.VERIFIER, "MOI")], False),
    ("e.approver", "Noor Al-Busaidi", "Budget Holder", [(Role.APPROVER, "MOE"), (Role.APPROVER, "MOI")], False),
    ("g.admin", "Said Al-Mamari", "Shared Services Administrator", [(Role.UNIT_ADMIN, "GOV")], False),
    ("auditor", "Rashid Al-Farsi", "Internal Auditor", [(Role.AUDITOR, "GOV")], False),
]


def seasonal(utility, month):
    """Hot summers drive electricity and water demand."""
    if utility in (UtilityType.ELECTRICITY,):
        return 1 + 0.45 * math.sin((month - 3) / 12 * 2 * math.pi)
    if utility == UtilityType.WATER:
        return 1 + 0.18 * math.sin((month - 3) / 12 * 2 * math.pi)
    return 1.0


def invoice_pdf(provider, account, number, issue, due, start, end, net, tax, total, consumption, unit):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, 790, provider.name)
    c.setFont("Helvetica", 10)
    lines = [
        "TAX INVOICE", f"Invoice No: {number}", f"Account No: {account.account_number}",
        f"Invoice Date: {issue:%d/%m/%Y}", f"Period: {start:%d/%m/%Y} to {end:%d/%m/%Y}",
        f"Consumption: {consumption} {unit}", f"Amount before tax: {net:,.3f}", f"VAT (5%): {tax:,.3f}",
        f"Total Amount Due: OMR {total:,.3f}", f"Due Date: {due:%d/%m/%Y}",
        "Premises: " + account.premises,
    ]
    y = 750
    for line in lines:
        c.drawString(60, y, line)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


class Command(BaseCommand):
    help = "Load demonstration data (development only)."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=26)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, months, force, **options):
        if settings.PRODUCTION:
            raise CommandError("Refusing to load demo data in production.")
        if Invoice.objects.exists() and not force:
            raise CommandError("Data already present. Use --force on an empty database only.")
        random.seed(2026)
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        call_command("setup_govbills", name=UNITS[0][3], code="GOV")
        with transaction.atomic():
            units = self._units()
            providers = self._providers()
            users = self._users(units)
            accounts = self._accounts(units, providers)
            for d, name in [(date(2026, 11, 18), "National Day"), (date(2026, 11, 19), "National Day"),
                            (date(2027, 1, 1), "New Year")]:
                Holiday.objects.get_or_create(date=d, defaults={"name": name})
        self._history(accounts, users, months)
        self._live(accounts, users)
        self.stdout.write(self.style.SUCCESS(
            f"Demo ready. Password for every demo user: {DEMO_PASSWORD} · signing PIN: {DEMO_PIN} · "
            f"TOTP secret: {DEMO_TOTP}"))

    def _units(self):
        units = {}
        for code, parent, kind, name, local in UNITS:
            unit, _ = OrgUnit.objects.get_or_create(code=code, defaults={
                "parent": units.get(parent), "kind": kind, "name": name, "name_local": local})
            if unit.name != name:
                unit.name, unit.kind, unit.name_local = name, kind, local
                unit.save()
            units[code] = unit
        return units

    def _providers(self):
        out = {}
        for name, code, utility, iban, holder, bic in PROVIDERS:
            p, _ = Provider.objects.get_or_create(short_code=code, defaults={
                "name": name, "utility_type": utility, "bank_iban": iban, "bank_account_name": holder,
                "bank_bic": bic, "bank_name": "Demo Bank", "currency": "OMR",
                "payment_terms_days": 30 if utility in (UtilityType.TELECOM, UtilityType.INTERNET) else 45})
            out[code] = p
        return out

    def _users(self, units):
        out = {}
        for username, name, title, roles, superuser in USERS:
            user, created = User.objects.get_or_create(username=username, defaults={
                "full_name": name, "email": f"{username}@demo.gov.example", "job_title": title,
                "employee_id": f"E{abs(hash(username)) % 100000:05d}", "is_superuser": superuser, "is_staff": superuser})
            if created:
                user.set_password(DEMO_PASSWORD)
                user.totp_secret = DEMO_TOTP
                user.totp_confirmed_at = timezone.now()
                user.save()
                RecoveryCode.regenerate(user)
            for role, code in roles:
                Membership.objects.get_or_create(user=user, org_unit=units[code], role=role)
            out[username] = user
        for username in ("m.approver", "k.director", "f.finance", "a.authoriser", "e.approver"):
            enroll(out[username], DEMO_PIN)
        return out

    def _accounts(self, units, providers):
        out = []
        for unit, prov, number, premises, amount, consumption in ACCOUNTS:
            acct, _ = ServiceAccount.objects.get_or_create(provider=providers[prov], account_number=number, defaults={
                "org_unit": units[unit], "premises": premises, "meter_number": f"M{number[-5:]}",
                "tariff": "Government", "budget_line": f"{unit}-UTL"})
            out.append((acct, amount, consumption))
        return out

    def _make(self, acct, base, base_cons, period_start, users, status, live=False):
        provider = acct.provider
        period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        factor = seasonal(provider.utility_type, period_start.month) * random.uniform(0.92, 1.08)
        consumption = Decimal(str(round(base_cons * factor, 0)))
        net = Decimal(str(round(base * factor, 3)))
        tax = (net * Decimal("0.05")).quantize(Decimal("0.001"))
        total = net + tax
        issue = period_end + timedelta(days=random.randint(3, 8))
        due = issue + timedelta(days=provider.payment_terms_days)
        number = f"{provider.short_code}-{period_end:%y%m}-{acct.account_number[-4:]}"
        clerk = users["h.clerk"] if acct.org_unit.code.startswith("MOH") else users["e.clerk"]
        inv = Invoice(service_account=acct, invoice_number=number, issue_date=issue, due_date=due,
                      period_start=period_start, period_end=period_end, amount_net=net, amount_tax=tax,
                      amount_total=total, consumption=consumption, currency="OMR")
        pdf = invoice_pdf(provider, acct, number, issue, due, period_start, period_end, net, tax, total, consumption,
                          provider.consumption_unit)
        import hashlib

        inv.document.save(f"{number}.pdf", ContentFile(pdf), save=False)
        inv.document_name = f"{number}.pdf"
        inv.document_sha256 = hashlib.sha256(pdf).hexdigest()
        save_invoice(inv, clerk)
        created = timezone.make_aware(timezone.datetime.combine(issue + timedelta(days=2), timezone.datetime.min.time())) \
            + timedelta(hours=9)
        Invoice.objects.filter(pk=inv.pk).update(created_at=created)
        return inv, clerk, created

    def _history(self, accounts, users, months):
        """Paid history with simulated approval tasks (fast path, no signatures)."""
        today = timezone.localdate()
        first = (today.replace(day=1) - timedelta(days=months * 31)).replace(day=1)
        cutoff = (today.replace(day=1) - timedelta(days=1)).replace(day=1) - timedelta(days=1)
        for acct, base, cons in accounts:
            start = first
            while start <= cutoff:
                inv, clerk, created = self._make(acct, base, cons, start, users, "paid")
                moh = acct.org_unit.code.startswith("MOH")
                steps = [("Verification of consumption & account", Role.VERIFIER, 8, users["s.verifier" if moh else "e.verifier"]),
                         ("Budget holder approval", Role.APPROVER, 16, users["m.approver" if moh else "e.approver"])]
                t = created + timedelta(hours=random.randint(2, 20))
                for order, (name, role, sla, who) in enumerate(steps, 1):
                    took = random.choice([3, 5, 6, 9, 12, 14, 20, 30]) if order == 2 else random.choice([2, 4, 6, 7, 11])
                    decided = t + timedelta(hours=took)
                    due_at = t + timedelta(hours=sla * 1.8)
                    ApprovalTask.objects.create(invoice=inv, round=1, order=order, name=name, role=role,
                                                escalation_role=Role.UNIT_ADMIN, sla_hours=sla, status="approved",
                                                activated_at=t, due_at=due_at, decided_at=decided, decided_by=who,
                                                breached=decided > due_at)
                    t = decided
                paid = inv.due_date - timedelta(days=random.choice([1, 2, 3, 4, 6, 8])) \
                    if random.random() < 0.94 else inv.due_date + timedelta(days=random.randint(1, 9))
                Invoice.objects.filter(pk=inv.pk).update(
                    status=Invoice.Status.PAID, submitted_at=created, approved_at=t, paid_at=paid,
                    payment_reference=f"TRX{random.randint(100000, 999999)}", flags=[])
                start = (start + timedelta(days=32)).replace(day=1)
        self.stdout.write("History created.")

    def _live(self, accounts, users):
        """Recent months go through the real engine, leaving work in every state."""
        today = timezone.localdate()
        last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        for i, (acct, base, cons) in enumerate(accounts):
            moh = acct.org_unit.code.startswith("MOH")
            verifier = users["s.verifier" if moh else "e.verifier"]
            approver = users["m.approver" if moh else "e.approver"]
            senior = users["k.director"] if moh else None
            for start in (last_month,):
                spike = i in (0, 13)
                inv, clerk, _ = self._make(acct, base * (1.9 if spike else 1), cons * (1.9 if spike else 1), start, users, "")
                stage = [0, 1, 2, 3, 5, 5, 4, 5][i % 8]
                if stage == 0:
                    continue  # draft
                engine.submit(inv, clerk)
                if stage == 1:
                    continue  # waiting for verifier
                task = inv.approval_tasks.get(order=1, round=1)
                if stage == 2:
                    engine.decide(task, verifier, engine.Decision.RETURN, "Meter reading differs from site log – please confirm with provider.")
                    continue
                engine.decide(task, verifier, engine.Decision.APPROVE, "Consumption checked against BMS.")
                if stage == 3:
                    continue  # waiting for budget holder
                task = inv.approval_tasks.get(order=2, round=1)
                engine.decide(task, approver, engine.Decision.APPROVE,
                              "Approved." if not inv.flags else "Spike explained by chiller replacement works.", pin=DEMO_PIN)
                inv.refresh_from_db()
                nxt = inv.approval_tasks.filter(status=ApprovalTask.Status.PENDING).first()
                if nxt and senior and stage == 5:
                    engine.decide(nxt, senior, engine.Decision.APPROVE, "High value – approved.", pin=DEMO_PIN)
        self._settlements(users)
        # Age one pending task past its SLA so escalation is visible.
        pending = ApprovalTask.objects.filter(status=ApprovalTask.Status.PENDING).order_by("pk").first()
        if pending:
            ApprovalTask.objects.filter(pk=pending.pk).update(
                activated_at=timezone.now() - timedelta(days=4), due_at=timezone.now() - timedelta(hours=6))
        call_command("run_scheduler")
        self.stdout.write("Live work created.")

    def _settlements(self, users):
        from settlements import services as settle

        moh, moe = OrgUnit.objects.get(code="MOH"), OrgUnit.objects.get(code="MOE")
        soon = timezone.localdate() + timedelta(days=30)
        # MOH telecom & data: fully executed, signed by preparer and authoriser.
        ids = list(settle.eligible_invoices(moh, "OMR", soon).filter(provider__short_code__in=["TNC", "FLD", "MWM"])
                   .values_list("pk", flat=True))
        if ids:
            si = settle.create_instruction(users["f.finance"], moh, "OMR", soon, ids, "Monthly utilities run.")
            settle.submit_for_authorisation(si, users["f.finance"], DEMO_PIN)
            settle.authorise(si, users["a.authoriser"], DEMO_PIN)
            settle.mark_sent(si, users["f.finance"])
            settle.mark_executed(si, users["f.finance"], "TRSY-2026-10-0412", timezone.localdate())
        # MOE: awaiting authorisation.
        ids = list(settle.eligible_invoices(moe, "OMR", soon).values_list("pk", flat=True))
        if ids:
            si = settle.create_instruction(users["f.finance"], moe, "OMR", soon, ids)
            settle.submit_for_authorisation(si, users["f.finance"], DEMO_PIN)
