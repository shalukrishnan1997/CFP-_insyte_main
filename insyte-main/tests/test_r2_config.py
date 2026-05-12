# ruff: noqa: E402

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add project root to path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Mock Django settings before importing anything that uses them
from django.conf import settings

if not settings.configured:
    settings.configure(
        R2_ACCOUNT_ID="test-account",
        R2_ACCESS_KEY_ID="test-key",
        R2_SECRET_ACCESS_KEY="test-secret",
        R2_REGION="weur",
        R2_S3_ENDPOINT_URL="",
        R2_BUCKET_NAME="test-bucket",
        INSTALLED_APPS=[],
    )

from core.storage_backends import get_r2_client


class TestR2Config(unittest.TestCase):
    @patch("boto3.client")
    def test_get_r2_client_default_endpoint(self, mock_boto3: MagicMock) -> None:
        # Ensure R2_S3_ENDPOINT_URL is empty
        with (
            patch.object(settings, "R2_S3_ENDPOINT_URL", ""),
            patch.object(settings, "R2_ACCOUNT_ID", "myacc"),
            patch.object(settings, "R2_REGION", "weur"),
        ):
            get_r2_client()

            mock_boto3.assert_called_once()
            _args, kwargs = mock_boto3.call_args
            self.assertEqual(
                kwargs["endpoint_url"],
                "https://myacc.weur.r2.cloudflarestorage.com",
            )

    @patch("boto3.client")
    def test_get_r2_client_custom_endpoint(self, mock_boto3: MagicMock) -> None:
        custom_url = "https://custom.r2.endpoint.com"
        with patch.object(settings, "R2_S3_ENDPOINT_URL", custom_url):
            get_r2_client()

            mock_boto3.assert_called_once()
            _args, kwargs = mock_boto3.call_args
            self.assertEqual(kwargs["endpoint_url"], custom_url)

    @patch("boto3.client")
    def test_get_r2_client_invalid_region_fallback(self, mock_boto3: MagicMock) -> None:
        with patch.object(settings, "R2_REGION", "eu"):
            get_r2_client()

            mock_boto3.assert_called_once()
            _args, kwargs = mock_boto3.call_args
            # 'eu' should fall back to 'auto' in region_name
            self.assertEqual(kwargs["region_name"], "auto")


if __name__ == "__main__":
    unittest.main()
