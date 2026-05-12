import logging
from collections.abc import Sequence
from typing import Any

import resend
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMessage

logger = logging.getLogger(__name__)


class ResendBackend(BaseEmailBackend):
    """
    A Django email backend that sends messages via Resend.
    """

    def __init__(self, fail_silently: bool = False, **kwargs: Any) -> None:
        super().__init__(fail_silently=fail_silently)

        self.api_key = getattr(settings, "RESEND_API_KEY", None)
        if not self.api_key:
            if not self.fail_silently:
                raise ImproperlyConfigured("RESEND_API_KEY is not set in settings.")
            logger.error("RESEND_API_KEY is not set in settings.")

        resend.api_key = self.api_key

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        """
        Send one or more EmailMessage objects and return the number of email
        messages sent.
        """
        if not email_messages:
            return 0

        sent_count = 0
        for message in email_messages:
            if self._send(message):
                sent_count += 1
        return sent_count

    def _send(self, email_message: EmailMessage) -> bool:
        """
        Send a single EmailMessage object.
        """
        if not email_message.recipients():
            return False

        try:
            is_html = email_message.content_subtype == "html"
            params: dict[str, Any] = {
                "from": email_message.from_email or settings.DEFAULT_FROM_EMAIL,
                "to": email_message.to,
                "subject": email_message.subject,
            }
            if is_html:
                params["html"] = email_message.body
            else:
                params["text"] = email_message.body

            # Add CC and BCC if present
            if email_message.cc:
                params["cc"] = email_message.cc
            if email_message.bcc:
                params["bcc"] = email_message.bcc

            # Handle alternative parts (e.g., if there's both plain text and HTML)
            if hasattr(email_message, "alternatives") and email_message.alternatives:
                for content, mimetype in email_message.alternatives:
                    if mimetype == "text/html":
                        params["html"] = content

            # Send via Resend SDK
            resend.Emails.send(params)  # type: ignore[arg-type]
            return True

        except Exception as e:
            if not self.fail_silently:
                raise
            logger.error(f"Failed to send email via Resend: {e}")
            return False
