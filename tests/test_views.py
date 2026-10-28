"""Smoke tests: every main page renders for a signed-in user, and dual control holds."""

import pyotp
import pytest
from django.urls import reverse

from providers.models import BankDetailsChange
from tests.conftest import PASSWORD, PIN, make_user


def sign_in(client, user):
    client.post(reverse("accounts:login"), {"username": user.username, "password": PASSWORD})
    client.post(reverse("accounts:otp_verify"), {"code": pyotp.TOTP(user.totp_secret).now()})


@pytest.mark.parametrize("name", [
    "core:dashboard", "core:notifications", "invoices:list", "invoices:create", "imports:list", "workflow:inbox",
    "workflow:sla", "workflow:policies", "settlements:list", "settlements:new", "analytics", "providers:list",
    "org:tree", "signatures:mine", "signatures:verify", "accounts:profile", "core:audit", "accounts:user_list",
])
def test_pages_render_for_superuser(client, tree, people, policy, make_invoice, name):
    make_invoice()
    admin = make_user("root", superuser=True)
    sign_in(client, admin)
    response = client.get(reverse(name))
    assert response.status_code == 200, response.content[:500]


def test_invoice_detail_and_task_pages(client, signers, policy, make_invoice):
    from workflow import engine

    inv = make_invoice()
    engine.submit(inv, signers["clerk"])
    sign_in(client, signers["verifier"])
    task = inv.approval_tasks.get(status="pending")
    assert client.get(reverse("invoices:detail", args=[inv.pk])).status_code == 200
    r = client.post(reverse("workflow:task", args=[task.pk]), {"decision": "approve", "comment": "ok"})
    assert r.status_code == 302
    task.refresh_from_db()
    assert task.status == "approved"


def test_auditor_cannot_reach_user_admin(client, tree):
    from org.models import Role

    auditor = make_user("aud", [(Role.AUDITOR, tree["gov"])])
    sign_in(client, auditor)
    assert client.get(reverse("accounts:user_list")).status_code == 403
    assert client.get(reverse("core:audit")).status_code == 200


def test_bank_details_need_second_admin(client, tree, provider):
    from org.models import Role

    a1 = make_user("a1", [(Role.UNIT_ADMIN, tree["gov"])])
    a2 = make_user("a2", [(Role.UNIT_ADMIN, tree["gov"])])
    sign_in(client, a1)
    r = client.post(reverse("providers:bank_change", args=[provider.pk]), {
        "bank_name": "New Bank", "bank_bic": "NBOMOMRX", "bank_iban": "GB82 WEST 1234 5698 7654 32",
        "bank_account_name": "Power Co", "evidence_note": "Called known contact"})
    assert r.status_code == 302
    change = BankDetailsChange.objects.get()
    client.post(reverse("providers:bank_decide", args=[provider.pk, change.pk]), {"decision": "approve"})
    provider.refresh_from_db()
    assert provider.bank_iban == "OM810180000001299123456"  # own request not applied
    client.post(reverse("accounts:logout"))
    sign_in(client, a2)
    client.post(reverse("providers:bank_decide", args=[provider.pk, change.pk]), {"decision": "approve"})
    provider.refresh_from_db()
    assert provider.bank_iban == "GB82WEST12345698765432"


def test_invalid_iban_rejected(client, tree, provider):
    from org.models import Role

    a1 = make_user("a1", [(Role.UNIT_ADMIN, tree["gov"])])
    sign_in(client, a1)
    r = client.post(reverse("providers:bank_change", args=[provider.pk]), {
        "bank_name": "X", "bank_iban": "GB00WEST12345698765432", "bank_account_name": "Y", "evidence_note": "z"})
    assert r.status_code == 200 and b"checksum" in r.content


def test_csv_export_neutralises_formulas(client, people, make_invoice, account):
    from invoices.models import Invoice

    inv = make_invoice()
    Invoice.objects.filter(pk=inv.pk).update(invoice_number="=HYPERLINK(\"http://evil\")")
    sign_in(client, people["clerk"])
    body = client.get(reverse("invoices:list") + "?export=csv").content
    assert b"'=HYPERLINK" in body


def test_signature_enrolment_requires_password(client, people):
    sign_in(client, people["approver"])
    r = client.post(reverse("signatures:mine"), {"form": "enroll", "pin": PIN, "pin_confirm": PIN, "password": "nope"})
    assert b"Wrong password" in r.content
    r = client.post(reverse("signatures:mine"), {"form": "enroll", "pin": PIN, "pin_confirm": PIN, "password": PASSWORD})
    assert r.status_code == 302
    assert people["approver"].signing_identities.filter(is_active=True).exists()
