"""Tests for production-settings hardening from the 2026-05-02 audit.

These tests import ``responsehandling.settings.production`` directly and
inspect its module-level attributes. They do not run under the normal
``DJANGO_SETTINGS_MODULE`` (test settings) so we monkeypatch ``os.environ``
to provide the env vars production refuses to start without.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture()
def production_settings(monkeypatch: pytest.MonkeyPatch):
    """Import production.py with a minimal valid env, then re-import for each test."""
    monkeypatch.setenv("SECRET_KEY", "x" * 60)
    monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("FIELD_ENCRYPTION_SALT", "test-salt-do-not-use")
    monkeypatch.setenv("SCAN_WEBHOOK_SECRET", "test-scan-webhook-secret")
    sys.modules.pop("responsehandling.settings.production", None)
    yield importlib.import_module("responsehandling.settings.production")
    sys.modules.pop("responsehandling.settings.production", None)


class TestAxesProxyConfig:
    """Production must inform axes of the reverse-proxy hop count (audit §1.7)."""

    def test_axes_proxy_count_defaults_to_one(self, production_settings) -> None:
        """Default AXES_PROXY_COUNT=1 matches the single-proxy deployment."""
        assert production_settings.AXES_PROXY_COUNT == 1

    def test_axes_ipware_proxy_count_mirrors_axes_proxy_count(
        self, production_settings
    ) -> None:
        """Both axes proxy-count knobs must agree to avoid silent disagreement."""
        assert (
            production_settings.AXES_IPWARE_PROXY_COUNT
            == production_settings.AXES_PROXY_COUNT
        )

    def test_axes_meta_precedence_prefers_forwarded_for(
        self, production_settings
    ) -> None:
        """X-Forwarded-For must be checked before REMOTE_ADDR."""
        assert production_settings.AXES_META_PRECEDENCE_ORDER == (
            "HTTP_X_FORWARDED_FOR",
            "REMOTE_ADDR",
        )

    def test_axes_proxy_count_can_be_overridden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operators with a 2-proxy chain can set AXES_PROXY_COUNT=2."""
        monkeypatch.setenv("SECRET_KEY", "x" * 60)
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("FIELD_ENCRYPTION_SALT", "test-salt-do-not-use")
        monkeypatch.setenv("SCAN_WEBHOOK_SECRET", "test-scan-webhook-secret")
        monkeypatch.setenv("AXES_PROXY_COUNT", "2")
        sys.modules.pop("responsehandling.settings.production", None)
        prod = importlib.import_module("responsehandling.settings.production")
        try:
            assert prod.AXES_PROXY_COUNT == 2
            assert prod.AXES_IPWARE_PROXY_COUNT == 2
        finally:
            sys.modules.pop("responsehandling.settings.production", None)


class TestRequiredSecrets:
    """Production must refuse to start without the secrets it depends on (audit §1.7)."""

    def test_missing_scan_webhook_secret_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Importing production with no SCAN_WEBHOOK_SECRET aborts the start-up."""
        monkeypatch.setenv("SECRET_KEY", "x" * 60)
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("FIELD_ENCRYPTION_SALT", "test-salt-do-not-use")
        monkeypatch.delenv("SCAN_WEBHOOK_SECRET", raising=False)
        sys.modules.pop("responsehandling.settings.production", None)
        with pytest.raises(ValueError, match="SCAN_WEBHOOK_SECRET"):
            importlib.import_module("responsehandling.settings.production")
        sys.modules.pop("responsehandling.settings.production", None)

    def test_missing_secret_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SECRET_KEY enforcement is unchanged — guard against accidental regression."""
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("FIELD_ENCRYPTION_SALT", "test-salt-do-not-use")
        monkeypatch.setenv("SCAN_WEBHOOK_SECRET", "test")
        sys.modules.pop("responsehandling.settings.production", None)
        with pytest.raises(ValueError, match="SECRET_KEY"):
            importlib.import_module("responsehandling.settings.production")
        sys.modules.pop("responsehandling.settings.production", None)

    def test_missing_field_encryption_salt_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FIELD_ENCRYPTION_SALT enforcement is unchanged."""
        monkeypatch.setenv("SECRET_KEY", "x" * 60)
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.delenv("FIELD_ENCRYPTION_SALT", raising=False)
        monkeypatch.setenv("SCAN_WEBHOOK_SECRET", "test")
        sys.modules.pop("responsehandling.settings.production", None)
        with pytest.raises(ValueError, match="FIELD_ENCRYPTION_SALT"):
            importlib.import_module("responsehandling.settings.production")
        sys.modules.pop("responsehandling.settings.production", None)
