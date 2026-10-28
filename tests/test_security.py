import time
from datetime import date, datetime, timedelta

import pyotp
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.models import RecoveryCode
from core.files import validate_upload
from core.models import AuditEvent, Holiday
from core.worktime import add_working_hours
from org.access import has_role, scope, units_for
from org.models import OrgUnit, Role
from tests.conftest import PASSWORD, make_user, pdf_bytes


def login(client, user, code=None):
    client.post(reverse("accounts:login"), {"username": user.username, "password": PASSWORD})
    return client.post(reverse("accounts:otp_verify"), {"code": code or pyotp.TOTP(user.totp_secret).now()})


# --- Authentication -------------------------------------------------------------

def test_password_alone_does_not_grant_access(client, people):
    client.post(reverse("accounts:login"), {"username": "clerk", "password": PASSWORD})
    response = client.get(reverse("invoices:list"))
    assert response.status_code == 302 and "2fa" in response.url


def test_totp_login_succeeds_and_code_cannot_be_replayed(client, people):
    user = people["clerk"]
    code = pyotp.TOTP(user.totp_secret).now()
    response = login(client, user, code)
    assert response.status_code == 302
    assert client.get(reverse("invoices:list")).status_code == 200
    client.post(reverse("accounts:logout"))
    response = login(client, user, code)  # same code again
    assert response.status_code == 200 and b"not valid" in response.content


def test_lockout_after_repeated_failures(client, people, settings):
    for _ in range(settings.LOGIN_MAX_FAILURES):
        client.post(reverse("accounts:login"), {"username": "clerk", "password": "wrong"})
    response = client.post(reverse("accounts:login"), {"username": "clerk", "password": PASSWORD})
    assert response.status_code == 200 and b"temporarily locked" in response.content
    assert AuditEvent.objects.filter(action="auth.login_locked").exists()


def test_recovery_code_is_single_use(client, people):
    user = people["clerk"]
    codes = RecoveryCode.regenerate(user)
    client.post(reverse("accounts:login"), {"username": "clerk", "password": PASSWORD})
    r = client.post(reverse("accounts:otp_verify"), {"code": codes[0], "use_recovery": "on"})
    assert r.status_code == 302
    client.post(reverse("accounts:logout"))
    client.post(reverse("accounts:login"), {"username": "clerk", "password": PASSWORD})
    r = client.post(reverse("accounts:otp_verify"), {"code": codes[0], "use_recovery": "on"})
    assert r.status_code == 200


def test_new_user_must_enrol_two_factor(client, tree):
    user = make_user("newbie", [(Role.VIEWER, tree["moh"])])
    user.totp_secret, user.totp_confirmed_at = "", None
    user.save()
    client.post(reverse("accounts:login"), {"username": "newbie", "password": PASSWORD})
    assert client.get(reverse("core:dashboard")).url == reverse("accounts:otp_enroll")


def test_idle_session_expires(client, people, settings):
    login(client, people["clerk"])
    session = client.session
    session["last_activity"] = int(time.time()) - settings.SESSION_IDLE_TIMEOUT_MINUTES * 60 - 5
    session.save()
    response = client.get(reverse("invoices:list"))
    assert response.status_code == 302 and reverse("accounts:login") in response.url


def test_forced_password_change(client, people):
    user = people["clerk"]
    user.must_change_password = True
    user.save()
    login(client, user)
    assert client.get(reverse("invoices:list")).url == reverse("accounts:password_change")


def test_security_headers(client, people):
    login(client, people["clerk"])
    r = client.get(reverse("core:dashboard"))
    assert "script-src 'self'" in r["Content-Security-Policy"]
    assert r["X-Frame-Options"] == "DENY"
    assert r["Cache-Control"] == "no-store"


# --- Row-level access -------------------------------------------------------------

def test_roles_apply_to_subtree_only(tree, people):
    clerk = people["clerk"]
    assert has_role(clerk, tree["hosp"], [Role.CLERK])
    assert not has_role(clerk, tree["moe"], [Role.CLERK])
    assert not has_role(clerk, tree["gov"], [Role.CLERK])
    assert set(units_for(clerk)) == {tree["moh"], tree["hosp"]}


def test_expired_delegation_is_ignored(tree):
    user = make_user("acting", [])
    from org.models import Membership

    Membership.objects.create(user=user, org_unit=tree["moh"], role=Role.APPROVER,
                              valid_until=timezone.localdate() - timedelta(days=1))
    assert not has_role(user, tree["moh"], [Role.APPROVER])


def test_out_of_scope_invoice_is_hidden(client, people, make_invoice):
    inv = make_invoice()
    login(client, people["outsider"])
    assert client.get(reverse("invoices:detail", args=[inv.pk])).status_code == 404
    assert client.get(reverse("invoices:document", args=[inv.pk])).status_code == 404
    from invoices.models import Invoice

    assert not scope(Invoice.objects.all(), people["outsider"]).exists()


def test_moving_a_unit_rewrites_descendant_paths(tree):
    hosp = tree["hosp"]
    hosp.parent = tree["moe"]
    hosp.save()
    assert OrgUnit.objects.get(pk=hosp.pk).path == f"{tree['moe'].path}{hosp.pk}/"


# --- Audit chain ------------------------------------------------------------------

def test_audit_chain_detects_tampering(db):
    for i in range(5):
        AuditEvent.record("test.event", data={"i": i})
    assert AuditEvent.verify_chain()[0]
    with pytest.raises(PermissionDenied):
        AuditEvent.objects.first().save()
    # Simulate a DBA editing a row directly.
    AuditEvent.objects.filter(sequence=3).update(data={"i": 99})
    ok, bad, _ = AuditEvent.verify_chain()
    assert not ok and bad == 3


def test_audit_chain_detects_deletion(db):
    for i in range(4):
        AuditEvent.record("test.event", data={"i": i})
    AuditEvent.objects.filter(sequence=4).delete()  # queryset delete bypasses the model guard
    assert not AuditEvent.verify_chain()[0]


# --- Uploads --------------------------------------------------------------------

def test_upload_validation_uses_content_not_name(db):
    fake = SimpleUploadedFile("invoice.pdf", b"MZ\x90\x00 this is an executable", content_type="application/pdf")
    with pytest.raises(ValidationError):
        validate_upload(fake, {"pdf"})
    real = SimpleUploadedFile("x.pdf", pdf_bytes())
    kind, digest = validate_upload(real, {"pdf"})
    assert kind == "pdf" and len(digest) == 64


def test_zip_renamed_as_xlsx_is_rejected(db):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.txt", "hello")
    with pytest.raises(ValidationError):
        validate_upload(SimpleUploadedFile("data.xlsx", buf.getvalue()), {"xlsx"})


def test_encrypted_fields_are_not_stored_in_clear(provider):
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT bank_iban FROM providers_provider WHERE id = %s", [provider.pk])
        raw = cur.fetchone()[0]
    assert raw.startswith("enc:") and provider.bank_iban not in raw


# --- Working time --------------------------------------------------------------------

def test_working_hours_skip_weekends_and_holidays(db, settings):
    settings.WORK_WEEK_DAYS = [6, 0, 1, 2, 3]  # Sunday–Thursday
    settings.WORK_DAY_START_HOUR, settings.WORK_DAY_END_HOUR = 7, 15
    tz = timezone.get_current_timezone()
    thursday_2pm = timezone.make_aware(datetime(2026, 10, 29, 14, 0), tz)
    # 1h left Thursday, Friday+Saturday off, Sunday 1 Nov is a holiday, so 2h lands Monday 08:00.
    Holiday.objects.create(date=date(2026, 11, 1), name="Test holiday")
    result = timezone.localtime(add_working_hours(thursday_2pm, 2), tz)
    assert result == timezone.make_aware(datetime(2026, 11, 2, 8, 0), tz)
