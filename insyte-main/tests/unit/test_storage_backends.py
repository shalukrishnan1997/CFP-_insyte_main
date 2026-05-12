"""Unit tests for core.storage_backends — R2 storage utilities."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

R2_SETTINGS = {
    "R2_BUCKET_NAME": "test-bucket",
    "R2_ACCESS_KEY_ID": "test-access-key",
    "R2_SECRET_ACCESS_KEY": "test-secret-key",
    "R2_ACCOUNT_ID": "test-account-id",
    "R2_REGION": "auto",
    "R2_S3_ENDPOINT_URL": "https://test.r2.cloudflarestorage.com",
}


class TestR2Enabled:
    """Tests for r2_enabled()."""

    @override_settings(
        R2_BUCKET_NAME="bucket",
        R2_ACCESS_KEY_ID="key",
        R2_SECRET_ACCESS_KEY="secret",
        R2_ACCOUNT_ID="account",
    )
    def test_returns_true_when_all_settings_configured(self) -> None:
        from core.storage_backends import r2_enabled

        assert r2_enabled() is True

    @override_settings(
        R2_BUCKET_NAME="",
        R2_ACCESS_KEY_ID="key",
        R2_SECRET_ACCESS_KEY="secret",
        R2_ACCOUNT_ID="account",
    )
    def test_returns_false_when_bucket_name_missing(self) -> None:
        from core.storage_backends import r2_enabled

        assert r2_enabled() is False

    @override_settings(
        R2_BUCKET_NAME="bucket",
        R2_ACCESS_KEY_ID="",
        R2_SECRET_ACCESS_KEY="secret",
        R2_ACCOUNT_ID="account",
    )
    def test_returns_false_when_access_key_missing(self) -> None:
        from core.storage_backends import r2_enabled

        assert r2_enabled() is False

    @override_settings(
        R2_BUCKET_NAME="bucket",
        R2_ACCESS_KEY_ID="key",
        R2_SECRET_ACCESS_KEY="",
        R2_ACCOUNT_ID="account",
    )
    def test_returns_false_when_secret_key_missing(self) -> None:
        from core.storage_backends import r2_enabled

        assert r2_enabled() is False

    @override_settings(
        R2_BUCKET_NAME="bucket",
        R2_ACCESS_KEY_ID="key",
        R2_SECRET_ACCESS_KEY="secret",
        R2_ACCOUNT_ID="",
    )
    def test_returns_false_when_account_id_missing(self) -> None:
        from core.storage_backends import r2_enabled

        assert r2_enabled() is False


class TestR2PublicUrl:
    """Tests for r2_public_url()."""

    @override_settings(R2_CUSTOM_DOMAIN="https://cdn.example.com", **R2_SETTINGS)
    def test_uses_custom_domain_with_https_prefix(self) -> None:
        from core.storage_backends import r2_public_url

        url = r2_public_url("scans/form-001.pdf")
        assert url == "https://cdn.example.com/scans/form-001.pdf"

    @override_settings(R2_CUSTOM_DOMAIN="cdn.example.com", **R2_SETTINGS)
    def test_uses_custom_domain_without_scheme(self) -> None:
        from core.storage_backends import r2_public_url

        url = r2_public_url("scans/form-001.pdf")
        assert url == "https://cdn.example.com/scans/form-001.pdf"

    @override_settings(
        R2_CUSTOM_DOMAIN="",
        R2_BUCKET_NAME="my-bucket",
        R2_ACCESS_KEY_ID="k",
        R2_SECRET_ACCESS_KEY="s",
        R2_ACCOUNT_ID="a",
    )
    def test_falls_back_to_default_url_when_no_custom_domain(self) -> None:
        from core.storage_backends import r2_public_url

        url = r2_public_url("scans/form-001.pdf")
        assert "my-bucket" in url
        assert "scans/form-001.pdf" in url

    @override_settings(R2_CUSTOM_DOMAIN="https://cdn.example.com", **R2_SETTINGS)
    def test_encodes_spaces_in_key(self) -> None:
        from core.storage_backends import r2_public_url

        url = r2_public_url("scans/my form.pdf")
        assert "my%20form.pdf" in url

    @override_settings(R2_CUSTOM_DOMAIN="https://cdn.example.com/", **R2_SETTINGS)
    def test_custom_domain_trailing_slash_handled(self) -> None:
        from core.storage_backends import r2_public_url

        url = r2_public_url("scans/form.pdf")
        # Should not produce double slash
        assert "cdn.example.com/scans/form.pdf" in url


class TestR2KeyExists:
    """Tests for r2_key_exists()."""

    def test_returns_false_when_r2_not_enabled(self) -> None:
        from core.storage_backends import r2_key_exists

        with override_settings(R2_BUCKET_NAME=""):
            result = r2_key_exists("some/key.pdf")
        assert result is False

    @override_settings(**R2_SETTINGS)
    def test_returns_true_when_object_exists(self) -> None:
        from core.storage_backends import r2_key_exists

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.head_object.return_value = {}
            mock_client_fn.return_value = mock_client
            result = r2_key_exists("scans/form-001.pdf")

        assert result is True

    @override_settings(**R2_SETTINGS)
    def test_returns_false_when_object_not_found(self) -> None:
        from core.storage_backends import r2_key_exists

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.head_object.side_effect = Exception("NoSuchKey")
            mock_client_fn.return_value = mock_client
            result = r2_key_exists("scans/nonexistent.pdf")

        assert result is False


class TestR2PresignedUrl:
    """Tests for r2_presigned_url()."""

    def setup_method(self) -> None:
        """Clear locmem cache between tests — r2_presigned_url caches results."""
        from django.core.cache import cache

        cache.clear()

    def test_returns_none_when_r2_not_enabled(self) -> None:
        from core.storage_backends import r2_presigned_url

        with override_settings(R2_BUCKET_NAME=""):
            result = r2_presigned_url("some/key.pdf")
        assert result is None

    @override_settings(**R2_SETTINGS)
    def test_returns_url_on_success(self) -> None:
        from core.storage_backends import r2_presigned_url

        expected_url = "https://test.r2.cloudflarestorage.com/scans/form-001.pdf?X-Amz-Signature=abc123"

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.generate_presigned_url.return_value = expected_url
            mock_client_fn.return_value = mock_client
            result = r2_presigned_url("scans/form-001.pdf")

        assert result == expected_url

    @override_settings(**R2_SETTINGS)
    def test_returns_none_on_exception(self) -> None:
        from core.storage_backends import r2_presigned_url

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.generate_presigned_url.side_effect = Exception("AWS error")
            mock_client_fn.return_value = mock_client
            result = r2_presigned_url("scans/form-001.pdf")

        assert result is None

    @override_settings(**R2_SETTINGS)
    def test_passes_expiry_to_client(self) -> None:
        from core.storage_backends import r2_presigned_url

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.generate_presigned_url.return_value = "https://example.com/key"
            mock_client_fn.return_value = mock_client
            r2_presigned_url("scans/form-001.pdf", expiry=7200)

        call_kwargs = mock_client.generate_presigned_url.call_args
        assert call_kwargs[1]["ExpiresIn"] == 7200


class TestR2ListPrefix:
    """Tests for r2_list_prefix()."""

    def test_returns_empty_when_r2_not_enabled(self) -> None:
        from core.storage_backends import r2_list_prefix

        with override_settings(R2_BUCKET_NAME=""):
            result = r2_list_prefix("scans/")
        assert result == []

    @override_settings(**R2_SETTINGS)
    def test_returns_keys_from_pages(self) -> None:
        from core.storage_backends import r2_list_prefix

        mock_page = {
            "Contents": [
                {"Key": "scans/form-001.pdf"},
                {"Key": "scans/form-002.pdf"},
            ]
        }

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [mock_page]
            mock_client.get_paginator.return_value = mock_paginator
            mock_client_fn.return_value = mock_client
            result = r2_list_prefix("scans/")

        assert result == ["scans/form-001.pdf", "scans/form-002.pdf"]

    @override_settings(**R2_SETTINGS)
    def test_propagates_unexpected_exceptions(self) -> None:
        """Unexpected (non-ClientError) failures must propagate so Celery retries."""
        from core.storage_backends import r2_list_prefix

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.get_paginator.side_effect = RuntimeError("network down")
            mock_client_fn.return_value = mock_client
            with pytest.raises(RuntimeError, match="network down"):
                r2_list_prefix("scans/")

    @override_settings(**R2_SETTINGS)
    def test_propagates_transient_client_errors(self) -> None:
        """5xx / throttling ClientErrors propagate for Celery retry."""
        from botocore.exceptions import ClientError

        from core.storage_backends import r2_list_prefix

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.side_effect = ClientError(
                {
                    "Error": {"Code": "InternalError", "Message": "We're sorry"},
                    "ResponseMetadata": {"HTTPStatusCode": 500},
                },
                "ListObjectsV2",
            )
            mock_client.get_paginator.return_value = mock_paginator
            mock_client_fn.return_value = mock_client
            with pytest.raises(ClientError):
                r2_list_prefix("scans/")

    @override_settings(**R2_SETTINGS)
    def test_raises_configuration_error_on_access_denied(self) -> None:
        """403 AccessDenied is a misconfig — raise non-retryable error."""
        from botocore.exceptions import ClientError

        from core.storage_backends import R2ConfigurationError, r2_list_prefix

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.side_effect = ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": "Forbidden"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "ListObjectsV2",
            )
            mock_client.get_paginator.return_value = mock_paginator
            mock_client_fn.return_value = mock_client
            with pytest.raises(R2ConfigurationError, match="AccessDenied"):
                r2_list_prefix("scans/")

    @override_settings(**R2_SETTINGS)
    def test_empty_pages_returns_empty(self) -> None:
        from core.storage_backends import r2_list_prefix

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Contents": []}]
            mock_client.get_paginator.return_value = mock_paginator
            mock_client_fn.return_value = mock_client
            result = r2_list_prefix("scans/empty/")

        assert result == []


class TestR2GetObject:
    """Tests for r2_get_object()."""

    def test_raises_when_r2_not_enabled(self) -> None:
        from core.storage_backends import r2_get_object

        with (
            override_settings(R2_BUCKET_NAME=""),
            pytest.raises(RuntimeError, match="R2 storage is not configured"),
        ):
            r2_get_object("scans/form.pdf")

    @override_settings(**R2_SETTINGS)
    def test_returns_body_bytes_on_success(self) -> None:
        from core.storage_backends import r2_get_object

        body_stream = MagicMock()
        body_stream.read.return_value = b"binary-bytes"

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.get_object.return_value = {"Body": body_stream}
            mock_client_fn.return_value = mock_client
            result = r2_get_object("scans/form.pdf")

        assert result == b"binary-bytes"
        mock_client.get_object.assert_called_once_with(
            Bucket="test-bucket", Key="scans/form.pdf"
        )

    @override_settings(**R2_SETTINGS)
    def test_propagates_boto_exceptions(self) -> None:
        from core.storage_backends import r2_get_object

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.get_object.side_effect = RuntimeError("NoSuchKey")
            mock_client_fn.return_value = mock_client
            with pytest.raises(RuntimeError, match="NoSuchKey"):
                r2_get_object("scans/missing.pdf")


class TestGetR2Client:
    """Tests for get_r2_client()."""

    @override_settings(**R2_SETTINGS)
    def test_creates_boto3_client(self) -> None:
        from core.storage_backends import get_r2_client

        with patch("boto3.client") as mock_boto3_client:
            mock_client = MagicMock()
            mock_boto3_client.return_value = mock_client
            result = get_r2_client()

        assert result == mock_client
        mock_boto3_client.assert_called_once()

    @override_settings(
        R2_S3_ENDPOINT_URL="",
        R2_REGION="",
        R2_ACCOUNT_ID="test-account",
        R2_ACCESS_KEY_ID="k",
        R2_SECRET_ACCESS_KEY="s",
        R2_BUCKET_NAME="b",
    )
    def test_constructs_endpoint_url_when_not_set(self) -> None:
        from core.storage_backends import get_r2_client

        with patch("boto3.client") as mock_boto3_client:
            mock_boto3_client.return_value = MagicMock()
            get_r2_client()

        call_kwargs = mock_boto3_client.call_args[1]
        assert "r2.cloudflarestorage.com" in call_kwargs["endpoint_url"]
