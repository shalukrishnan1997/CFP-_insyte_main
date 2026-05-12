"""Unit tests for the Document AI hardening unit (#19).

Covers:

* Transient errors (``ResourceExhausted``) are retried by tenacity and the
  call eventually succeeds.
* Permanent errors (``InvalidArgument``) escape the retry decorator on the
  first attempt — no quota is burned re-trying broken inputs.
* PDFs over ``MAX_OCR_PAGES_PER_PDF`` are rejected by the
  ``/webhooks/scan-upload/`` endpoint with HTTP 413 *before* the OCR
  Celery task is enqueued.
* The Document AI client is cached at module level keyed on
  ``(project_id, location, credentials_hash)`` so two OCR calls within the
  TTL window reuse a single OAuth handshake.
"""

import hashlib
import hmac
import json
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import django
import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

django.setup()

from scans import document_ai as documentai_module  # noqa: E402
from scans.document_ai import (  # noqa: E402
    DocumentAIResult,
    DocumentAIService,
    PDFTooLargeError,
    _reset_client_cache,
)
from scans.scan_processing_r2 import count_pdf_pages  # noqa: E402
from tests.factories import CampaignFactory, ClientFactory  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _scan_signature(secret: str, payload: bytes, timestamp: str) -> str:
    """Return HMAC-SHA256 signature over ``f"{timestamp}.{payload}"``."""
    signed = timestamp.encode() + b"." + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def _mock_documentai_module() -> Any:
    """Build a minimal fake ``google.cloud.documentai`` module."""
    mock_documentai = MagicMock()
    mock_documentai.RawDocument.return_value = MagicMock()
    mock_documentai.ProcessRequest.return_value = MagicMock()
    return mock_documentai


def _mock_response(text: str = "donation") -> Any:
    """Build a minimal mock Document AI response."""
    doc = MagicMock()
    doc.text = text
    doc.entities = []
    doc.pages = [MagicMock()]
    doc.pages[0].form_fields = []
    response = MagicMock()
    response.document = doc
    return response


def _client_obj() -> Any:
    """Build a SimpleNamespace stand-in for the Client model."""
    c = SimpleNamespace()
    c.name = "Test Charity"
    c.document_ai_processor_id = "abc123"
    c.document_ai_location = "eu"
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Retry behaviour
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessImageBytesRetry:
    """Tests for tenacity-managed retry around ``process_document``."""

    @patch("scans.document_ai.settings")
    def test_transient_resource_exhausted_is_retried_then_succeeds(
        self, mock_settings: Any
    ) -> None:
        """``ResourceExhausted`` (429) once, then 200 — tenacity retries and wins."""
        from google.api_core.exceptions import ResourceExhausted

        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        mock_settings.DOCUMENT_AI_MAX_RETRY_ATTEMPTS = 5
        mock_settings.DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS = 3600
        mock_settings.MAX_OCR_PAGES_PER_PDF = 100

        mock_doc_ai = MagicMock()
        # First call: 429. Second call: 200.
        mock_doc_ai.process_document.side_effect = [
            ResourceExhausted("rate limit"),
            _mock_response("ok"),
        ]

        with (
            patch.dict(
                sys.modules, {"google.cloud.documentai": _mock_documentai_module()}
            ),
            patch(
                "scans.document_ai.DocumentAIService._get_client",
                return_value=mock_doc_ai,
            ),
            # Replace tenacity's wait with no-op so the test runs instantly.
            patch(
                "scans.document_ai.wait_exponential_jitter", return_value=lambda _: 0
            ),
        ):
            result = DocumentAIService.process_image_bytes(
                b"\xff\xd8\xff", _client_obj()
            )

        assert isinstance(result, DocumentAIResult)
        assert mock_doc_ai.process_document.call_count == 2

    @patch("scans.document_ai.settings")
    def test_permanent_invalid_argument_is_not_retried(
        self, mock_settings: Any
    ) -> None:
        """``InvalidArgument`` is permanent — no retry, surfaces as RuntimeError."""
        from google.api_core.exceptions import InvalidArgument

        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        mock_settings.DOCUMENT_AI_MAX_RETRY_ATTEMPTS = 5
        mock_settings.DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS = 3600
        mock_settings.MAX_OCR_PAGES_PER_PDF = 100

        mock_doc_ai = MagicMock()
        mock_doc_ai.process_document.side_effect = InvalidArgument("malformed PDF")

        with (
            patch.dict(
                sys.modules, {"google.cloud.documentai": _mock_documentai_module()}
            ),
            patch(
                "scans.document_ai.DocumentAIService._get_client",
                return_value=mock_doc_ai,
            ),
            pytest.raises(RuntimeError, match="malformed PDF"),
        ):
            DocumentAIService.process_image_bytes(b"\xff\xd8\xff", _client_obj())

        # Permanent error → exactly one attempt, no retry.
        assert mock_doc_ai.process_document.call_count == 1

    @patch("scans.document_ai.settings")
    def test_permanent_permission_denied_is_not_retried(
        self, mock_settings: Any
    ) -> None:
        """``PermissionDenied`` is permanent — surfaces immediately."""
        from google.api_core.exceptions import PermissionDenied

        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        mock_settings.DOCUMENT_AI_MAX_RETRY_ATTEMPTS = 5
        mock_settings.DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS = 3600
        mock_settings.MAX_OCR_PAGES_PER_PDF = 100

        mock_doc_ai = MagicMock()
        mock_doc_ai.process_document.side_effect = PermissionDenied("forbidden")

        with (
            patch.dict(
                sys.modules, {"google.cloud.documentai": _mock_documentai_module()}
            ),
            patch(
                "scans.document_ai.DocumentAIService._get_client",
                return_value=mock_doc_ai,
            ),
            pytest.raises(RuntimeError, match="forbidden"),
        ):
            DocumentAIService.process_image_bytes(b"\xff\xd8\xff", _client_obj())

        assert mock_doc_ai.process_document.call_count == 1

    @patch("scans.document_ai.settings")
    def test_transient_retries_exhausted_raises_transient_ocr_error(
        self, mock_settings: Any
    ) -> None:
        """When all attempts fail with transient errors, surface a
        ``TransientOCRError`` so the per-scan Celery task can retry with a
        fresh worker / longer backoff. (Tenacity covers seconds-scale
        flapping; Celery covers minutes-scale region/queue outages.)
        """
        from google.api_core.exceptions import ServiceUnavailable

        from scans.document_ai import TransientOCRError

        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        mock_settings.DOCUMENT_AI_MAX_RETRY_ATTEMPTS = 3
        mock_settings.DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS = 3600
        mock_settings.MAX_OCR_PAGES_PER_PDF = 100

        mock_doc_ai = MagicMock()
        mock_doc_ai.process_document.side_effect = ServiceUnavailable("503")

        with (
            patch.dict(
                sys.modules, {"google.cloud.documentai": _mock_documentai_module()}
            ),
            patch(
                "scans.document_ai.DocumentAIService._get_client",
                return_value=mock_doc_ai,
            ),
            patch(
                "scans.document_ai.wait_exponential_jitter", return_value=lambda _: 0
            ),
            pytest.raises(TransientOCRError, match="failed after retries"),
        ):
            DocumentAIService.process_image_bytes(b"\xff\xd8\xff", _client_obj())

        assert mock_doc_ai.process_document.call_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# PDF page-count limit
# ─────────────────────────────────────────────────────────────────────────────


class TestPDFPageLimit:
    """Tests for ``MAX_OCR_PAGES_PER_PDF`` enforcement."""

    @patch("scans.document_ai.settings")
    def test_oversize_pdf_raises_pdf_too_large_error(self, mock_settings: Any) -> None:
        """Direct OCR call on a 101-page PDF raises PDFTooLargeError."""
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        mock_settings.MAX_OCR_PAGES_PER_PDF = 100
        mock_settings.DOCUMENT_AI_MAX_RETRY_ATTEMPTS = 5

        with (
            patch("scans.scan_processing_r2.count_pdf_pages", return_value=101),
            patch(
                "scans.document_ai.DocumentAIService._get_client",
                return_value=MagicMock(),
            ),
            patch.dict(
                sys.modules, {"google.cloud.documentai": _mock_documentai_module()}
            ),
            pytest.raises(PDFTooLargeError) as exc_info,
        ):
            DocumentAIService.process_image_bytes(b"%PDF-1.4 fake", _client_obj())

        assert exc_info.value.page_count == 101
        assert exc_info.value.limit == 100

    def test_count_pdf_pages_returns_none_for_garbage(self) -> None:
        """Unparseable bytes yield ``None`` rather than raising."""
        assert count_pdf_pages(b"not a pdf") is None


@pytest.mark.django_db()
class TestPDFPageLimitWebhook:
    """Tests for ``/webhooks/scan-upload/`` 413 page-limit response."""

    def test_oversize_pdf_in_prefix_returns_413(
        self, client: Client, settings: SettingsWrapper
    ) -> None:
        """When the prefix contains a PDF with > MAX_OCR_PAGES_PER_PDF pages, 413."""
        settings.SCAN_WEBHOOK_SECRET = "scan-secret-test"
        settings.MAX_OCR_PAGES_PER_PDF = 100

        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict: dict[str, Any] = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "latest_urn": "Big.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Big.pdf",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        with (
            patch(
                "scans.scan_folder._list_all_keys_under_prefix",
                return_value=["ScanOutput/BRC/SPRING25/cheque/Big.pdf"],
            ),
            patch("core.storage_backends.r2_enabled", return_value=True),
            patch("core.storage_backends.get_r2_client") as mock_r2,
            patch(
                "scans.scan_processing_r2.count_pdf_pages",
                return_value=101,
            ),
            patch("scans.tasks.create_scan_batch_from_r2_task.delay") as mock_delay,
        ):
            mock_r2.return_value.get_object.return_value = {
                "Body": MagicMock(read=lambda: b"%PDF-1.4 fake")
            }
            response = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )

        assert response.status_code == 413
        body = response.json()
        assert "exceeds MAX_OCR_PAGES_PER_PDF=100" in body["error"]
        # OCR task must NOT have been enqueued.
        mock_delay.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Client cache (single OAuth handshake for two OCRs in TTL window)
# ─────────────────────────────────────────────────────────────────────────────


class TestClientCache:
    """Tests for the module-level Document AI client cache."""

    def setup_method(self) -> None:
        _reset_client_cache()

    def teardown_method(self) -> None:
        _reset_client_cache()

    @patch("scans.document_ai.settings")
    def test_two_ocr_requests_share_one_oauth_handshake(
        self, mock_settings: Any
    ) -> None:
        """Two OCR calls within the cache TTL trigger exactly one ``_build_client`` call."""
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        mock_settings.DOCUMENT_AI_MAX_RETRY_ATTEMPTS = 5
        mock_settings.DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS = 3600
        mock_settings.MAX_OCR_PAGES_PER_PDF = 100

        sentinel_client = MagicMock()
        sentinel_client.process_document.return_value = _mock_response()

        with (
            patch.dict(
                sys.modules, {"google.cloud.documentai": _mock_documentai_module()}
            ),
            patch(
                "scans.document_ai.DocumentAIService._build_client",
                return_value=sentinel_client,
            ) as mock_build,
        ):
            DocumentAIService.process_image_bytes(b"\xff\xd8\xff", _client_obj())
            DocumentAIService.process_image_bytes(b"\xff\xd8\xff", _client_obj())

        # Single OAuth handshake even though two OCRs happened.
        assert mock_build.call_count == 1
        assert sentinel_client.process_document.call_count == 2

    @patch("scans.document_ai.settings")
    def test_credential_rotation_invalidates_cache(self, mock_settings: Any) -> None:
        """Changing the credential JSON env var rebuilds the client."""
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        mock_settings.DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS = 3600

        with (
            patch(
                "scans.document_ai.DocumentAIService._build_client",
                return_value=MagicMock(),
            ) as mock_build,
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_APPLICATION_CREDENTIALS_JSON": '{"v": 1}',
                    "GOOGLE_APPLICATION_CREDENTIALS_JSON_B64": "",
                    "GOOGLE_APPLICATION_CREDENTIALS": "",
                },
            ),
        ):
            DocumentAIService._get_client("eu")
            DocumentAIService._get_client("eu")
            assert mock_build.call_count == 1

            # Rotate credentials → next call must rebuild.
            with patch.dict(
                "os.environ",
                {"GOOGLE_APPLICATION_CREDENTIALS_JSON": '{"v": 2}'},
            ):
                DocumentAIService._get_client("eu")
                assert mock_build.call_count == 2

    @patch("scans.document_ai.settings")
    def test_cache_ttl_expiry_rebuilds_client(self, mock_settings: Any) -> None:
        """Stale cache entries are rebuilt after TTL elapses."""
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        mock_settings.DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS = 60

        # First build: read at t=0 (store). Second call: read at t=30 (fresh).
        # Third call: read at t=121 (stale → rebuild → store at t=121).
        clock = {"now": 0.0}

        def fake_monotonic() -> float:
            return clock["now"]

        with (
            patch(
                "scans.document_ai.DocumentAIService._build_client",
                return_value=MagicMock(),
            ) as mock_build,
            patch.object(
                documentai_module.time, "monotonic", side_effect=fake_monotonic
            ),
        ):
            clock["now"] = 0.0
            DocumentAIService._get_client("eu")  # build at t=0
            clock["now"] = 30.0
            DocumentAIService._get_client("eu")  # at t=30, still fresh
            clock["now"] = 121.0
            DocumentAIService._get_client("eu")  # at t=121, rebuild

        assert mock_build.call_count == 2
