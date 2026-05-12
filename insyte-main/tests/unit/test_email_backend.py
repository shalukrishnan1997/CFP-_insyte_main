from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage, EmailMultiAlternatives

from core.mail_backends import ResendBackend


class TestResendBackend:
    @patch("resend.Emails.send")
    def test_send_single_email(
        self, mock_resend_send: MagicMock, settings: Any
    ) -> None:
        settings.RESEND_API_KEY = "test_key"
        backend = ResendBackend()
        email = EmailMessage(
            subject="Test Subject",
            body="Test Body",
            from_email="from@example.com",
            to=["to@example.com"],
        )

        result = backend.send_messages([email])

        assert result == 1
        mock_resend_send.assert_called_once_with(
            {
                "from": "from@example.com",
                "to": ["to@example.com"],
                "subject": "Test Subject",
                "text": "Test Body",
            }
        )

    @patch("resend.Emails.send")
    def test_send_html_email(self, mock_resend_send: MagicMock, settings: Any) -> None:
        settings.RESEND_API_KEY = "test_key"
        backend = ResendBackend()
        email = EmailMessage(
            subject="Test Subject",
            body="<h1>Test Body</h1>",
            from_email="from@example.com",
            to=["to@example.com"],
        )
        email.content_subtype = "html"

        result = backend.send_messages([email])

        assert result == 1
        mock_resend_send.assert_called_once_with(
            {
                "from": "from@example.com",
                "to": ["to@example.com"],
                "subject": "Test Subject",
                "html": "<h1>Test Body</h1>",
            }
        )

    @patch("resend.Emails.send")
    def test_send_multi_alternatives_email(
        self, mock_resend_send: MagicMock, settings: Any
    ) -> None:
        settings.RESEND_API_KEY = "test_key"
        backend = ResendBackend()
        email = EmailMultiAlternatives(
            subject="Test Subject",
            body="Text Body",
            from_email="from@example.com",
            to=["to@example.com"],
        )
        email.attach_alternative("<h1>HTML Body</h1>", "text/html")

        result = backend.send_messages([email])

        assert result == 1
        mock_resend_send.assert_called_once_with(
            {
                "from": "from@example.com",
                "to": ["to@example.com"],
                "subject": "Test Subject",
                "html": "<h1>HTML Body</h1>",
                "text": "Text Body",
            }
        )

    def test_missing_api_key_raises_error(self, settings: Any) -> None:
        settings.RESEND_API_KEY = None
        with pytest.raises(ImproperlyConfigured):
            ResendBackend(fail_silently=False)

    @patch("resend.Emails.send")
    def test_send_failure_fail_silently(
        self, mock_resend_send: MagicMock, settings: Any
    ) -> None:
        settings.RESEND_API_KEY = "test_key"
        mock_resend_send.side_effect = Exception("Resend generic error")
        backend = ResendBackend(fail_silently=True)
        email = EmailMessage(subject="S", body="B", to=["t@e.com"])

        result = backend.send_messages([email])

        assert result == 0
