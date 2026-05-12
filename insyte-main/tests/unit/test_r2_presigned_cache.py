"""Regression tests for the R2 presigned-URL cache layer.

Under load (100 QA reviewers x ~5 scans per page = 500 page-load image
fetches), the previous implementation issued one boto3
``generate_presigned_url`` call per request. These tests pin the contract
that consecutive calls for the same key share a cached URL and only hit
boto3 once.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import override_settings

R2_SETTINGS = {
    "R2_BUCKET_NAME": "test-bucket",
    "R2_ACCESS_KEY_ID": "test-access-key",
    "R2_SECRET_ACCESS_KEY": "test-secret-key",
    "R2_ACCOUNT_ID": "test-account-id",
    "R2_REGION": "auto",
    "R2_S3_ENDPOINT_URL": "https://test.r2.cloudflarestorage.com",
}


class TestR2PresignedUrlCache:
    """Verify ``r2_presigned_url`` reuses cached URLs across calls."""

    def setup_method(self) -> None:
        """Clear the locmem cache between tests to avoid cross-test bleed."""
        cache.clear()

    @override_settings(**R2_SETTINGS)
    def test_repeat_calls_hit_boto3_only_once(self) -> None:
        """Two calls with the same key should produce one boto3 roundtrip."""
        from core.storage_backends import r2_presigned_url

        signed_url = (
            "https://test.r2.cloudflarestorage.com/scans/form-001.pdf"
            "?X-Amz-Signature=cached"
        )

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.generate_presigned_url.return_value = signed_url
            mock_client_fn.return_value = mock_client

            first = r2_presigned_url("scans/form-001.pdf")
            second = r2_presigned_url("scans/form-001.pdf")

            assert first == signed_url
            assert second == signed_url
            assert mock_client.generate_presigned_url.call_count == 1

    @override_settings(**R2_SETTINGS)
    def test_different_keys_each_hit_boto3(self) -> None:
        """Distinct keys must not share cache entries."""
        from core.storage_backends import r2_presigned_url

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.generate_presigned_url.side_effect = [
                "https://example.com/a?sig=1",
                "https://example.com/b?sig=2",
            ]
            mock_client_fn.return_value = mock_client

            url_a = r2_presigned_url("scans/a.pdf")
            url_b = r2_presigned_url("scans/b.pdf")

            assert url_a == "https://example.com/a?sig=1"
            assert url_b == "https://example.com/b?sig=2"
            assert mock_client.generate_presigned_url.call_count == 2

    @override_settings(**R2_SETTINGS)
    def test_cache_ttl_subtracts_headroom_from_expiry(self) -> None:
        """The cache TTL is ``expiry - 600s`` so URLs expire before R2 does."""
        from core.storage_backends import r2_presigned_url

        signed_url = "https://example.com/key?sig=ttl"

        with (
            patch("core.storage_backends.get_r2_client") as mock_client_fn,
            patch("core.storage_backends.cache", autospec=False) as mock_cache,
        ):
            mock_client = MagicMock()
            mock_client.generate_presigned_url.return_value = signed_url
            mock_client_fn.return_value = mock_client
            mock_cache.get.return_value = None

            r2_presigned_url("scans/x.pdf", expiry=3600)

        # The default 1-hour expiry should give a 50-minute cache TTL.
        mock_cache.set.assert_called_once_with("r2_url:scans/x.pdf", signed_url, 3000)

    @override_settings(**R2_SETTINGS)
    def test_failures_are_not_cached(self) -> None:
        """A boto3 failure returns None and must not poison the cache."""
        from core.storage_backends import r2_presigned_url

        with patch("core.storage_backends.get_r2_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.generate_presigned_url.side_effect = Exception("AWS down")
            mock_client_fn.return_value = mock_client

            first = r2_presigned_url("scans/broken.pdf")

            assert first is None
            # Recover: subsequent call should retry boto3, not return cached None.
            mock_client.generate_presigned_url.side_effect = None
            mock_client.generate_presigned_url.return_value = "https://ok/x?sig=2"
            second = r2_presigned_url("scans/broken.pdf")

            assert second == "https://ok/x?sig=2"
            assert mock_client.generate_presigned_url.call_count == 2
