from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.management import CommandError, call_command
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from config.preview_support import PreviewHeadersMiddleware
from core.middleware import SecurityHeadersMiddleware


def test_preview_embedding_is_limited_to_replit():
    request = RequestFactory().get("/accounts/login/")
    request.user = AnonymousUser()
    response = PreviewHeadersMiddleware(lambda request: HttpResponse("login"))(request)
    policy = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self' https://replit.com https://*.replit.com https://*.replit.dev" in policy
    assert "frame-ancestors *" not in policy
    assert "X-Frame-Options" not in response.headers
    assert "form-action 'self'" in policy


def test_normal_security_policy_still_blocks_embedding():
    request = RequestFactory().get("/accounts/login/")
    request.user = AnonymousUser()
    response = SecurityHeadersMiddleware(lambda request: HttpResponse("login"))(request)
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("production,preview", [(True, True), (False, False)])
def test_preview_initialization_refuses_non_preview_environments(production, preview):
    with override_settings(PRODUCTION=production, REPLIT_PREVIEW=preview):
        with patch("core.management.commands.prepare_replit_preview.call_command") as command:
            with pytest.raises(CommandError, match="only in the Replit development preview"):
                call_command("prepare_replit_preview")
            command.assert_not_called()
