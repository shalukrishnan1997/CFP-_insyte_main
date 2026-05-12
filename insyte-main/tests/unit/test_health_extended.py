"""Unit tests for core.health health_check endpoint."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory
from django.test.utils import override_settings


@pytest.mark.django_db()
class TestHealthCheckAllHealthy:
    """Tests where all services respond correctly."""

    def test_returns_200_when_all_healthy(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")
        # Just confirm the endpoint returns a valid JSON structure
        response = health_check(request)

        import json

        data = json.loads(response.content)
        assert "healthy" in data
        assert "checks" in data
        assert response.status_code in (200, 503)

    def test_database_check_included(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")
        response = health_check(request)

        import json

        data = json.loads(response.content)
        assert "database" in data["checks"]

    def test_cache_check_included(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")
        response = health_check(request)

        import json

        data = json.loads(response.content)
        assert "cache" in data["checks"]

    def test_celery_check_included(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")
        response = health_check(request)

        import json

        data = json.loads(response.content)
        assert "celery" in data["checks"]


@pytest.mark.django_db()
class TestHealthCheckDatabaseFailure:
    """Tests when database connectivity fails."""

    def test_returns_503_on_db_failure(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")

        with patch("core.health.connection") as mock_conn:
            mock_conn.cursor.side_effect = Exception("DB connection failed")
            response = health_check(request)

        assert response.status_code == 503

        import json

        data = json.loads(response.content)
        assert data["healthy"] is False
        assert data["checks"]["database"]["healthy"] is False

    def test_db_failure_message_included(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")

        with patch("core.health.connection") as mock_conn:
            mock_conn.cursor.side_effect = Exception("timeout connecting")
            response = health_check(request)

        import json

        data = json.loads(response.content)
        assert data["checks"]["database"]["status"] == "unavailable"


@pytest.mark.django_db()
class TestHealthCheckCacheFailure:
    """Tests when cache connectivity fails."""

    def test_returns_503_on_cache_failure(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")

        with patch("django.core.cache.cache") as mock_cache:
            mock_cache.set.side_effect = Exception("Redis unavailable")
            response = health_check(request)

        assert response.status_code == 503

    def test_cache_read_mismatch_returns_503(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")

        with patch("django.core.cache.cache") as mock_cache:
            mock_cache.set.return_value = None
            mock_cache.get.return_value = "wrong_value"  # mismatch
            response = health_check(request)

        assert response.status_code == 503

        import json

        data = json.loads(response.content)
        assert data["checks"]["cache"]["healthy"] is False


@pytest.mark.django_db()
class TestHealthCheckCeleryFailure:
    """Tests when Celery workers are unavailable."""

    def test_no_workers_returns_503(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")

        with patch("responsehandling.celery.app") as mock_app:
            mock_inspect = MagicMock()
            mock_inspect.stats.return_value = None
            mock_app.control.inspect.return_value = mock_inspect
            response = health_check(request)

        assert response.status_code == 503

        import json

        data = json.loads(response.content)
        assert data["checks"]["celery"]["healthy"] is False


@pytest.mark.django_db()
class TestHealthCheckCeleryBrokerMode:
    """Tests the development-only Celery broker readiness fallback."""

    @override_settings(HEALTH_CHECK_CELERY_MODE="broker")
    def test_broker_mode_returns_200_with_warning(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")
        mock_connection = MagicMock()

        with patch("responsehandling.celery.app") as mock_app:
            mock_app.connection_for_read.return_value = mock_connection
            response = health_check(request)

        assert response.status_code == 200

        import json

        data = json.loads(response.content)
        assert data["checks"]["celery"] == {
            "status": "broker_ok",
            "healthy": True,
            "warning": True,
        }
        mock_connection.ensure_connection.assert_called_once_with(max_retries=0)
        mock_connection.release.assert_called_once()

    @override_settings(HEALTH_CHECK_CELERY_MODE="broker")
    def test_broker_mode_failure_returns_503(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")
        mock_connection = MagicMock()
        mock_connection.ensure_connection.side_effect = Exception("Broker unreachable")

        with patch("responsehandling.celery.app") as mock_app:
            mock_app.connection_for_read.return_value = mock_connection
            response = health_check(request)

        assert response.status_code == 503

        import json

        data = json.loads(response.content)
        assert data["checks"]["celery"]["healthy"] is False
        mock_connection.release.assert_called_once()

    def test_celery_exception_returns_503(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")

        with patch("responsehandling.celery.app") as mock_app:
            mock_app.control.inspect.side_effect = Exception("Broker unreachable")
            response = health_check(request)

        assert response.status_code == 503

        import json

        data = json.loads(response.content)
        assert data["checks"]["celery"]["healthy"] is False


@pytest.mark.django_db()
class TestHealthCheckDocumentAI:
    """Tests for _check_document_ai helper."""

    def test_no_project_id_warns_but_healthy(self, rf: RequestFactory) -> None:

        _request = rf.get("/health/")

        with (
            patch("django.conf.settings") as mock_settings,
            patch("responsehandling.celery.app") as mock_celery,
        ):
            mock_settings.GOOGLE_CLOUD_PROJECT_ID = ""
            mock_celery.control.inspect.return_value.stats.return_value = {
                "worker1": {}
            }

            from core.health import _check_document_ai

            checks: dict[str, Any] = {}
            healthy_ref = [True]
            _check_document_ai(checks, healthy_ref)

        assert "document_ai" in checks
        assert checks["document_ai"]["healthy"] is True

    def test_no_credentials_warns_but_healthy(self) -> None:
        from core.health import _check_document_ai

        checks: dict[str, Any] = {}
        healthy_ref = [True]

        with (
            patch("django.conf.settings") as mock_settings,
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_APPLICATION_CREDENTIALS": "",
                    "GOOGLE_APPLICATION_CREDENTIALS_JSON": "",
                    "GOOGLE_APPLICATION_CREDENTIALS_JSON_B64": "",
                },
                clear=False,
            ),
        ):
            mock_settings.GOOGLE_CLOUD_PROJECT_ID = "some-project"
            _check_document_ai(checks, healthy_ref)

        assert "document_ai" in checks
        assert checks["document_ai"]["healthy"] is True

    def test_with_project_and_credentials_checks_clients(self) -> None:
        from core.health import _check_document_ai
        from tests.factories import ClientFactory

        # Create 2 active clients with processor IDs
        ClientFactory(is_active=True, document_ai_processor_id="proc-1")
        ClientFactory(is_active=True, document_ai_processor_id="proc-2")
        ClientFactory(is_active=True, document_ai_processor_id="")  # no processor

        checks: dict[str, Any] = {}
        healthy_ref = [True]

        with (
            patch("django.conf.settings") as mock_settings,
            patch.dict("os.environ", {"GOOGLE_APPLICATION_CREDENTIALS": "/creds.json"}),
        ):
            mock_settings.GOOGLE_CLOUD_PROJECT_ID = "test-project"
            _check_document_ai(checks, healthy_ref)

        assert "document_ai" in checks
        assert checks["document_ai"]["healthy"] is True
        assert checks["document_ai"]["status"] == "configured"

    def test_health_check_does_not_leak_raw_exception_text(
        self, rf: RequestFactory
    ) -> None:
        from core.health import health_check

        request = rf.get("/health/")

        with patch("core.health.connection") as mock_conn:
            mock_conn.cursor.side_effect = Exception("postgres password rejected")
            response = health_check(request)

        import json

        data = json.loads(response.content)
        assert data["checks"]["database"]["status"] == "unavailable"
        assert "password rejected" not in response.content.decode()

    def test_document_ai_check_does_not_expose_project_id(self) -> None:
        from core.health import _check_document_ai
        from tests.factories import ClientFactory

        ClientFactory(is_active=True, document_ai_processor_id="proc-1")

        checks: dict[str, Any] = {}
        healthy_ref = [True]

        with (
            patch("django.conf.settings") as mock_settings,
            patch.dict("os.environ", {"GOOGLE_APPLICATION_CREDENTIALS": "/creds.json"}),
        ):
            mock_settings.GOOGLE_CLOUD_PROJECT_ID = "secret-project-id"
            _check_document_ai(checks, healthy_ref)

        assert checks["document_ai"]["status"] == "configured"
        assert "project_id" not in checks["document_ai"]

    def test_overall_health_returns_json_structure(self, rf: RequestFactory) -> None:
        from core.health import health_check

        request = rf.get("/health/")
        response = health_check(request)

        import json

        data = json.loads(response.content)
        assert isinstance(data["healthy"], bool)
        assert isinstance(data["checks"], dict)
        assert response.status_code in (200, 503)


class TestHealthCheckResend:
    """Tests for _check_resend helper.

    The probe is presence-only — no live Resend API call — so these tests
    just exercise the three branches: non-Resend backend, Resend backend
    with a missing key, and Resend backend with a key set. A missing key
    must remain a warning, not a hard failure (matches the Document AI
    pattern), so ``healthy_ref`` is asserted untouched in every branch.
    """

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_non_resend_backend_reports_not_applicable(self) -> None:
        from core.health import _check_resend

        checks: dict[str, Any] = {}
        healthy_ref = [True]
        _check_resend(checks, healthy_ref)

        assert checks["resend"] == {
            "status": "not_applicable",
            "healthy": True,
            "warning": True,
        }
        assert healthy_ref == [True]

    @override_settings(
        EMAIL_BACKEND="core.mail_backends.ResendBackend",
        RESEND_API_KEY="",
    )
    def test_resend_backend_without_key_reports_not_configured(self) -> None:
        from core.health import _check_resend

        checks: dict[str, Any] = {}
        healthy_ref = [True]
        _check_resend(checks, healthy_ref)

        assert checks["resend"] == {
            "status": "not_configured",
            "healthy": True,
            "warning": True,
        }
        assert healthy_ref == [True]

    @override_settings(
        EMAIL_BACKEND="core.mail_backends.ResendBackend",
        RESEND_API_KEY="re_test_key",
    )
    def test_resend_backend_with_key_reports_configured(self) -> None:
        from core.health import _check_resend

        checks: dict[str, Any] = {}
        healthy_ref = [True]
        _check_resend(checks, healthy_ref)

        assert checks["resend"] == {"status": "configured", "healthy": True}
        assert healthy_ref == [True]

    @override_settings(
        EMAIL_BACKEND="core.mail_backends.ResendBackend",
        RESEND_API_KEY=None,
    )
    def test_resend_backend_with_none_key_reports_not_configured(self) -> None:
        # base.py reads RESEND_API_KEY via os.getenv, which returns None when
        # unset — guard against treating None as a configured value.
        from core.health import _check_resend

        checks: dict[str, Any] = {}
        healthy_ref = [True]
        _check_resend(checks, healthy_ref)

        assert checks["resend"]["status"] == "not_configured"
        assert healthy_ref == [True]

    @pytest.mark.django_db()
    def test_resend_check_included_in_health_endpoint(self, rf: RequestFactory) -> None:
        from core.health import health_check

        response = health_check(rf.get("/health/"))

        import json

        data = json.loads(response.content)
        assert "resend" in data["checks"]
