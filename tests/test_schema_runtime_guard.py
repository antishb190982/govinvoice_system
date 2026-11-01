"""Development schema preparation must never run in a published service."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.runtime_database import postgres_config, require_development_schema_context


@pytest.mark.parametrize("marker", ["", None])
def test_development_workspace_is_allowed(marker):
    require_development_schema_context("development", marker)


@pytest.mark.parametrize("environment", ["production", "staging", "", None])
def test_non_development_modes_are_rejected(environment):
    with pytest.raises(ImproperlyConfigured, match="development-only"):
        require_development_schema_context(environment, "")


@pytest.mark.parametrize("marker", ["1", "true", "production", "0", "unexpected"])
def test_deployment_marker_blocks_even_explicit_development_mode(marker):
    with pytest.raises(ImproperlyConfigured, match="development-only"):
        require_development_schema_context("development", marker)


def test_legacy_environment_marker_cannot_misclassify_the_workspace(monkeypatch):
    monkeypatch.setenv("REPLIT_ENVIRONMENT", "production")
    require_development_schema_context("development", "")


def test_postgres_transport_remains_secure_by_default():
    config = postgres_config("postgresql://localhost/schema_guard_test")
    assert config["OPTIONS"]["sslmode"] == "require"
