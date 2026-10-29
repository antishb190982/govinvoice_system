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

def mu_iban(bank, branch, account):
    """Build a checksum-valid 30-character Mauritian IBAN: MUkk + bank(4) + 2+2 digits + account(12) + 000 + MUR."""
    bban = f"{bank}{branch:0>4}{account:0>12}000MUR"
    digits = "".join(str(int(c, 36)) for c in bban + "MU00")
    return f"MU{98 - int(digits) % 97:02d}{bban}"


UNITS = [
    ("GOM", None, "government", "Government of Mauritius", "Gouvernement de Maurice"),
    ("PMO", "GOM", "ministry", "Prime Minister's Office", "Bureau du Premier ministre"),
    ("PMO-HQ", "PMO", "department", "PMO Headquarters, Port Louis", ""),
    ("MOHW", "GOM", "ministry", "Ministry of Health and Wellness", "Ministère de la Santé et du Bien-être"),
    ("MOHW-HOS", "MOHW", "directorate", "Regional Hospitals", ""),
    ("MOHW-HOS-JEE", "MOHW-HOS", "site", "Dr A. G. Jeetoo Hospital, Port Louis", ""),
    ("MOHW-HOS-SSRN", "MOHW-HOS", "site", "Sir Seewoosagur Ramgoolam National Hospital, Pamplemousses", ""),
    ("MOHW-PHC", "MOHW", "directorate", "Area Health Centres & Community Health", ""),
    ("MOE", "GOM", "ministry", "Ministry of Education and Human Resource", "Ministère de l'Éducation"),
    ("MOE-Z1", "MOE", "directorate", "Zone 1 – Port Louis schools", ""),
    ("MOE-Z4", "MOE", "directorate", "Zone 4 – Curepipe & Plaines Wilhems schools", ""),
    ("MOF", "GOM", "ministry", "Ministry of Finance", "Ministère des Finances"),
    ("MOF-TRY", "MOF", "department", "Treasury – Accountant-General", ""),
]

# name, code, utility, IBAN, beneficiary, BIC, VAT rate. Demo bank details are fictitious.
PROVIDERS = [
    ("Central Electricity Board", "CEB", UtilityType.ELECTRICITY, mu_iban("MCBL", "0901", "000440011"),
     "Central Electricity Board – Collections", "MCBLMUMU", Decimal("0")),
    ("Central Water Authority", "CWA", UtilityType.WATER, mu_iban("MCBL", "0901", "000551023"),
     "Central Water Authority – Revenue", "MCBLMUMU", Decimal("0")),
    ("Mauritius Telecom", "MT", UtilityType.TELECOM, mu_iban("MCBL", "0901", "000662034"),
     "Mauritius Telecom Ltd", "MCBLMUMU", Decimal("0.15")),
    ("Emtel Ltd", "EMTEL", UtilityType.INTERNET, mu_iban("MCBL", "0901", "000773045"),
     "Emtel Ltd – Business Billing", "MCBLMUMU", Decimal("0.15")),
    ("Wastewater Management Authority", "WMA", UtilityType.WASTE, mu_iban("MCBL", "0901", "000884056"),
     "Wastewater Management Authority", "MCBLMUMU", Decimal("0")),
]
PROVIDER_ALIASES = {"CEB": "CEB\nCentral Electricity Board (Mauritius)", "CWA": "CWA\nCentral Water Authority",
                    "MT": "Mauritius Telecom Ltd\nmy.t", "EMTEL": "Emtel\nEmtel Business",
                    "WMA": "WMA\nWastewater Management Authority (Mauritius)"}

# unit, provider, account, premises, base monthly amount (Rs), base consumption
ACCOUNTS = [
    ("MOHW-HOS-JEE", "CEB", "1100453201", "Main hospital block", 2150000, 410000),
    ("MOHW-HOS-JEE", "CEB", "1100453202", "Outpatient & cardiac centre", 720000, 138000),
    ("MOHW-HOS-JEE", "CWA", "80023117", "Hospital campus", 460000, 21000),
    ("MOHW-HOS-JEE", "MT", "2087001", "Switchboard & mobiles", 170000, 52000),
    ("MOHW-HOS-JEE", "EMTEL", "EB-554120", "Campus fibre 1 Gbps", 115000, 42000),
    ("MOHW-HOS-JEE", "WMA", "WMA-30011", "Sewerage – hospital campus", 245000, 38),
    ("MOHW-HOS-SSRN", "CEB", "1100678870", "Hospital building", 1880000, 360000),
    ("MOHW-HOS-SSRN", "CWA", "80044552", "Hospital building", 390000, 18800),
    ("MOHW-HOS-SSRN", "MT", "2087210", "Telephony", 61000, 18000),
    ("MOHW-PHC", "CEB", "1100810012", "Area health centres – North", 365000, 69000),
    ("MOHW-PHC", "CEB", "1100810013", "Area health centres – South", 330000, 62000),
    ("MOHW-PHC", "CWA", "80051200", "Area health centres", 75000, 3400),
    ("MOHW-PHC", "EMTEL", "EB-554300", "Clinic connectivity", 89000, 15000),
    ("MOE-Z1", "CEB", "1200110450", "Secondary schools – Port Louis", 1150000, 215000),
    ("MOE-Z1", "CEB", "1200110451", "Primary schools – Port Louis", 1020000, 192000),
    ("MOE-Z1", "CWA", "80110010", "Schools – Port Louis", 270000, 12500),
    ("MOE-Z1", "EMTEL", "EB-561000", "Schools broadband", 225000, 88000),
    ("MOE-Z4", "CEB", "1200540010", "Schools – Curepipe", 835000, 158000),
    ("MOE-Z4", "CWA", "80150300", "Schools – Curepipe", 141000, 6700),
    ("MOE-Z4", "WMA", "WMA-44100", "Sewerage – schools", 76000, 12),
    ("PMO-HQ", "CEB", "1300900001", "Government House & New Treasury Building", 1310000, 247000),
    ("PMO-HQ", "CWA", "80900100", "Government House", 165000, 7600),
    ("PMO-HQ", "MT", "2080001", "PMO telephony & mobiles", 305000, 91000),
    ("PMO-HQ", "EMTEL", "EB-590000", "Data centre links", 258000, 120000),
]

USERS = [
    # username, full name, title, [(role, unit)], superuser
    ("sysadmin", "System Administrator", "Government Online Centre", [], True),
    ("h.clerk", "Priya Ramdhany", "Accounts Officer", [(Role.CLERK, "MOHW")], False),
    ("s.verifier", "Jean-Marc Lagesse", "Hospital Engineer", [(Role.VERIFIER, "MOHW")], False),
    ("m.approver", "Anjali Seetaram", "Hospital Administrator", [(Role.APPROVER, "MOHW")], False),
    ("k.director", "Vikash Doorgakant", "Deputy Permanent Secretary", [(Role.APPROVER, "MOHW"), (Role.UNIT_ADMIN, "MOHW")], False),
    ("f.finance", "Marie-Claire Lebon", "Manager, Financial Operations", [(Role.FINANCE, "MOHW"), (Role.FINANCE, "MOE")], False),
    ("a.authoriser", "Rajesh Sookun", "Principal Financial Operations Officer", [(Role.AUTHORISER, "MOHW"), (Role.AUTHORISER, "MOE")], False),
    ("e.clerk", "Nadia Peerbux", "Accounts Officer", [(Role.CLERK, "MOE"), (Role.CLERK, "PMO")], False),
    ("e.verifier", "Kevin Ah-Kee", "Works Inspector", [(Role.VERIFIER, "MOE"), (Role.VERIFIER, "PMO")], False),
    ("e.approver", "Sandeep Bhoyroo", "Assistant Permanent Secretary", [(Role.APPROVER, "MOE"), (Role.APPROVER, "PMO")], False),
    ("g.admin", "Fabrice Perrine", "Shared Services Administrator", [(Role.UNIT_ADMIN, "GOM")], False),
    ("auditor", "Ashvin Gungadin", "Internal Control Officer", [(Role.AUDITOR, "GOM")], False),
]

HOLIDAYS = [  # fixed-date public holidays; add the lunar-calendar ones each year (Cavadee, Divali, Eid…)
    (date(2026, 1, 1), "New Year"), (date(2026, 1, 2), "New Year"), (date(2026, 2, 1), "Abolition of Slavery"),
    (date(2026, 3, 12), "Independence and Republic Day"), (date(2026, 5, 1), "Labour Day"),
    (date(2026, 8, 15), "Assumption of the Blessed Virgin Mary"),
    (date(2026, 11, 2), "Arrival of Indentured Labourers"), (date(2026, 12, 25), "Christmas"),
    (date(2027, 1, 1), "New Year"), (date(2027, 1, 2), "New Year"),
]


def seasonal(utility, month):
    """Southern-hemisphere summer (Dec–Mar) drives air-conditioning and water demand."""
    if utility == UtilityType.ELECTRICITY:
        return 1 + 0.28 * math.cos((month - 2) / 12 * 2 * math.pi)
    if utility == UtilityType.WATER:
        return 1 + 0.15 * math.cos((month - 2) / 12 * 2 * math.pi)
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
        f"Consumption: {consumption} {unit}", f"Amount before tax: {net:,.2f}", f"VAT: {tax:,.2f}",
        f"Total Amount Due: Rs {total:,.2f}", f"Due Date: {due:%d/%m/%Y}",
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
        call_command("setup_govbills", name=UNITS[0][3], code="GOM")
        with transaction.atomic():
            units = self._units()
            providers = self._providers()
            users = self._users(units)
            accounts = self._accounts(units, providers)
            for d, name in HOLIDAYS:
                Holiday.objects.get_or_create(date=d, defaults={"name": name})
        self._history(accounts, users, months)
        self._live(accounts, users)
        call_command("apply_provider_profiles")
        self._scans(users)
        self._integrations(users)
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
        for name, code, utility, iban, holder, bic, vat in PROVIDERS:
            p, _ = Provider.objects.get_or_create(short_code=code, defaults={
                "name": name, "utility_type": utility, "bank_iban": iban, "bank_account_name": holder,
                "bank_bic": bic, "bank_name": "The Mauritius Commercial Bank Ltd", "currency": "MUR",
                "identifiers": PROVIDER_ALIASES.get(code, ""),
                "payment_terms_days": 30 if utility in (UtilityType.TELECOM, UtilityType.INTERNET) else 45})
            if not p.identifiers:
                p.identifiers = PROVIDER_ALIASES.get(code, "")
                p.save(update_fields=["identifiers"])
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
        net = Decimal(str(round(base * factor, 2)))
        vat = next(v for _, code, *_rest, v in PROVIDERS if code == provider.short_code)
        tax = (net * vat).quantize(Decimal("0.01"))
        total = net + tax
        issue = period_end + timedelta(days=random.randint(3, 8))
        due = issue + timedelta(days=provider.payment_terms_days)
        number = f"{provider.short_code}-{period_end:%y%m}-{acct.account_number[-4:]}"
        clerk = users["h.clerk"] if acct.org_unit.code.startswith("MOHW") else users["e.clerk"]
        inv = Invoice(service_account=acct, invoice_number=number, issue_date=issue, due_date=due,
                      period_start=period_start, period_end=period_end, amount_net=net, amount_tax=tax,
                      amount_total=total, consumption=consumption, currency="MUR")
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
                moh = acct.org_unit.code.startswith("MOHW")
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
            moh = acct.org_unit.code.startswith("MOHW")
            verifier = users["s.verifier" if moh else "e.verifier"]
            approver = users["m.approver" if moh else "e.approver"]
            senior = users["k.director"] if moh else None
            for start in (last_month,):
                spike = i in (0, 13)
                inv, clerk, _ = self._make(acct, base * (1.9 if spike else 1), cons * (1.9 if spike else 1), start, users, "")
                # Demo-only provider evidence goes through the same importer as staff uploads.
                # Never automatically reconcile real workspace records on ordinary startup.
                import csv
                import io
                from django.core.files.base import ContentFile
                from reconciliation.services import upload_statement
                listing = io.StringIO()
                writer = csv.writer(listing)
                writer.writerow(["account_number", "invoice_number", "amount", "currency"])
                for pending in Invoice.objects.filter(provider=inv.provider, currency=inv.currency,
                        org_unit=inv.org_unit, status__in=Invoice.OPEN).select_related("service_account"):
                    writer.writerow([pending.service_account.account_number, pending.invoice_number,
                                     str(pending.amount_total), pending.currency])
                upload_statement(users["sysadmin"], inv.provider, inv.org_unit, inv.currency, today,
                                 ContentFile(listing.getvalue().encode(), name="demo-provider-pending.csv"))
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

    def _scans(self, users):
        """A staged batch of scanned / photographed invoices, to show the OCR review."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from imports.services import stage_documents
        from ocr.samples import as_bytes, as_scanned_pdf, invoice_image

        today = timezone.localdate()
        common = dict(issue=today - timedelta(days=4), due=today + timedelta(days=41),
                      start=(today.replace(day=1) - timedelta(days=1)).replace(day=1),
                      end=today.replace(day=1) - timedelta(days=1))
        files = [
            SimpleUploadedFile("CEB-scan-1100810012.jpg", as_bytes(invoice_image(
                provider_name="Central Electricity Board", account="1100810012", number="CEB/2610/0012",
                net="318,400.00", vat="0.00", total="318,400.00", consumption="66,940", prev="1,204,330",
                cur="1,271,270", skew=1.2, noise=0.002, blur=0.5, **common), "JPEG")),
            SimpleUploadedFile("CWA-photo-80051200.pdf", as_scanned_pdf(invoice_image(
                provider_name="Central Water Authority", account="80051200", number="CWA-10-51200", layout="stacked",
                net="71,250.00", vat="0.00", total="71,250.00", consumption="3,310", unit="m3", prev="88,120",
                cur="91,430", rotate90=True, **common))),
        ]
        batch = stage_documents(users["h.clerk"], files)
        self.stdout.write(f"OCR demo batch #{batch.pk} staged.")

    def _integrations(self, users):
        from integrations.models import ApiClient

        service, _ = User.objects.get_or_create(username="api:treasury-erp", defaults={
            "full_name": "API client – Treasury ERP (demo)", "email": "treasury-erp@api.invalid", "is_active": False})
        client = ApiClient(name="Treasury ERP (demo)", description="Reads approved invoices and settlement status",
                           scopes=["invoices:read", "settlements:read", "analytics:read", "mcp"],
                           service_user=service, created_by=users["sysadmin"])
        token = client.issue_token()
        client.save()
        self.stdout.write(f"Demo API/MCP token (shown once): {token}")

    def _settlements(self, users):
        from settlements import services as settle

        moh, moe = OrgUnit.objects.get(code="MOHW"), OrgUnit.objects.get(code="MOE")
        soon = timezone.localdate() + timedelta(days=30)
        # MOH telecom & data: fully executed, signed by preparer and authoriser.
        ids = list(settle.eligible_invoices(moh, "MUR", soon).filter(provider__short_code__in=["MT", "EMTEL", "WMA"])
                   .values_list("pk", flat=True))
        for provider_id in Invoice.objects.filter(pk__in=ids).order_by().values_list("provider_id", flat=True).distinct():
            provider_ids = list(Invoice.objects.filter(pk__in=ids, provider_id=provider_id).values_list("pk", flat=True))
            si = settle.create_instruction(users["f.finance"], moh, "MUR", soon, provider_ids, "Monthly utilities run — single provider.")
            settle.submit_for_authorisation(si, users["f.finance"], DEMO_PIN)
            settle.authorise(si, users["a.authoriser"], DEMO_PIN)
            settle.mark_sent(si, users["f.finance"])
            settle.mark_executed(si, users["f.finance"], f"TRY-EFT-2026-10-0412-{provider_id}", timezone.localdate())
        # MOE: awaiting authorisation.
        ids = list(settle.eligible_invoices(moe, "MUR", soon).values_list("pk", flat=True))
        for provider_id in Invoice.objects.filter(pk__in=ids).order_by().values_list("provider_id", flat=True).distinct():
            provider_ids = list(Invoice.objects.filter(pk__in=ids, provider_id=provider_id).values_list("pk", flat=True))
            si = settle.create_instruction(users["f.finance"], moe, "MUR", soon, provider_ids)
            settle.submit_for_authorisation(si, users["f.finance"], DEMO_PIN)
