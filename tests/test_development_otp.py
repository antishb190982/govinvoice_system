from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.urls import reverse

from accounts import security
from accounts.middleware import OTP_SESSION_KEY
from tests.conftest import PASSWORD


@pytest.mark.parametrize("production,fixed_code,accepted", [
    (False, "123456", True),
    (False, "", False),
    (True, "123456", True),
    (True, "", False),
])
def test_fixed_code_requires_explicit_setting(settings, monkeypatch, production, fixed_code, accepted):
    settings.PRODUCTION = production
    settings.FIXED_OTP_CODE = fixed_code
    real_totp = Mock()
    real_totp.interval = 30
    real_totp.verify.return_value = False
    monkeypatch.setattr(security.pyotp, "TOTP", Mock(return_value=real_totp))
    user = SimpleNamespace(totp_secret="example", totp_last_step=-1, save=Mock())
    assert security.verify_totp(user, "123456") is accepted
    assert security.verify_totp(user, "123456") is accepted
    user.save.assert_not_called()
    assert security.fixed_otp_code() == ("123456" if accepted else "")


@pytest.mark.django_db
def test_demo_form_prefills_code_and_completes_login(client, people, settings):
    settings.PRODUCTION = False
    settings.REPLIT_PREVIEW = True
    settings.FIXED_OTP_CODE = "123456"
    user = people["clerk"]
    response = client.post(reverse("accounts:login"), {"username": user.username, "password": PASSWORD})
    assert response.status_code == 302 and response.url == reverse("accounts:otp_verify")
    assert not client.session.get(OTP_SESSION_KEY)
    form_response = client.get(reverse("accounts:otp_verify"))
    assert form_response.context["form"]["code"].value() == "123456"
    assert b"Temporary verification" in form_response.content
    verified = client.post(reverse("accounts:otp_verify"), {"code": "123456"})
    assert verified.status_code == 302 and client.session[OTP_SESSION_KEY]
