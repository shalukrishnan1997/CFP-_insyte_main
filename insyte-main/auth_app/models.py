"""
auth_app models - Custom authentication models.

This module contains custom authentication models including email-based OTP devices.
"""

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import models
from django.utils import timezone
from django_otp.models import Device

logger = logging.getLogger(__name__)


class OTPThrottledError(Exception):
    """Raised when ``EmailDevice.generate_challenge`` is rate-limited.

    Callers should catch this and render a generic, non-enumerating message
    to the user (no remaining-time countdown — that would let an attacker
    enumerate the throttle window). See ``auth_app.views`` for usage.
    """


# ─── OTP issuance throttle (audit 2026-05-02 §1.3) ──────────────────────
# An attacker who knows a user's password can otherwise trigger unbounded OTP
# emails and brute-force the 6-digit code (3 attempts per email times N emails).
# axes only rate-limits *failed* logins, not successful-password→OTP-issuance,
# so we throttle OTP generation itself.
#
# Implementation note: the hourly/daily windows are *fixed* (TTL-bounded),
# not rolling. The hour-key TTL is 3600s from first issuance in that
# window; once it expires the counter resets. This means the worst-case
# burst at the hour boundary is ``2 * _OTP_HOURLY_LIMIT`` issuances in
# slightly more than a minute, which is acceptable for the brute-force
# defence here. A true rolling window would need a sorted-set / Redis
# Lua script and isn't worth the complexity for this throttle.
_OTP_MIN_INTERVAL_SECONDS = 60  # 1 issuance per 60 s
_OTP_HOURLY_LIMIT = 3  # 3 issuances per fixed hour window
_OTP_DAILY_LIMIT = 10  # 10 issuances per fixed 24h window


class EmailDevice(Device):
    """
    A two-factor authentication device that sends OTP codes via email.

    This device generates a random 6-digit code and emails it to the user.
    The code expires after 5 minutes.
    """

    # OTP code (hashed for security)
    token = models.CharField(max_length=64, blank=True, default="")

    # When the OTP was generated
    generated_at = models.DateTimeField(null=True, blank=True)

    # How many minutes until the OTP expires
    valid_minutes = models.IntegerField(default=5)

    # Maximum verification attempts
    max_attempts = models.IntegerField(default=3)

    # Current number of failed attempts
    failed_attempts = models.IntegerField(default=0)

    class Meta:
        db_table = "otp_emaildevice"
        verbose_name = "Email OTP Device"
        verbose_name_plural = "Email OTP Devices"

    def __str__(self):
        return f"Email OTP for {self.user.username}"

    def _throttle_keys(self) -> tuple[str, str, str]:
        """Return cache keys for the three OTP-issuance throttle windows."""
        # Use the user pk (stable, opaque) — never the username (mutable).
        suffix = str(self.user_id)
        return (
            f"otp:issue:last:{suffix}",
            f"otp:issue:hour:{suffix}",
            f"otp:issue:day:{suffix}",
        )

    def _check_throttle(self) -> None:
        """Raise ``OTPThrottledError`` if any OTP-issuance window is exhausted.

        Three fixed-TTL windows (60s / hour / day), all advisory
        (cache-backed; an evicted entry just opens the window). Tight
        enough to deter brute-force email-OTP guessing without locking
        out legitimate users who genuinely typo the code a couple of
        times in a session.
        """
        last_key, hour_key, day_key = self._throttle_keys()

        if cache.get(last_key):
            logger.warning(
                "OTP issuance throttled (60s window) for user_id=%s", self.user_id
            )
            raise OTPThrottledError("min_interval")

        hour_count = cache.get(hour_key, 0)
        if hour_count >= _OTP_HOURLY_LIMIT:
            logger.warning(
                "OTP issuance throttled (hourly limit) for user_id=%s count=%s",
                self.user_id,
                hour_count,
            )
            raise OTPThrottledError("hourly_limit")

        day_count = cache.get(day_key, 0)
        if day_count >= _OTP_DAILY_LIMIT:
            logger.warning(
                "OTP issuance throttled (daily limit) for user_id=%s count=%s",
                self.user_id,
                day_count,
            )
            raise OTPThrottledError("daily_limit")

    def _record_issuance(self) -> None:
        """Bump the throttle counters after a successful issuance."""
        last_key, hour_key, day_key = self._throttle_keys()
        cache.set(last_key, True, timeout=_OTP_MIN_INTERVAL_SECONDS)
        # Cache-backend assumption: ``cache.incr`` raises ``ValueError`` if
        # the key doesn't exist. This is the documented behaviour for
        # Django's LocMem (test) and Redis (prod) backends. A future
        # backend swap that uses a different exception (e.g. ``KeyError``)
        # would silently break counter initialisation — verify this
        # invariant if ``CACHES`` is reconfigured.
        try:
            cache.incr(hour_key)
        except ValueError:
            cache.set(hour_key, 1, timeout=3600)
        try:
            cache.incr(day_key)
        except ValueError:
            cache.set(day_key, 1, timeout=86400)

    def _reset_throttle(
        self,
        *,
        min_interval: bool = False,
        hourly: bool = False,
        daily: bool = False,
    ) -> None:
        """Clear specified OTP throttle windows.

        Test-only helper. Production code should let cache TTLs expire
        naturally — calling this from a request path would defeat the
        rate limit. Provided so tests don't have to reach into private
        ``_throttle_keys`` and the cache module directly.

        Args:
            min_interval: Drop the 60-second gate.
            hourly: Drop the hour-window counter.
            daily: Drop the day-window counter.
        """
        last_key, hour_key, day_key = self._throttle_keys()
        if min_interval:
            cache.delete(last_key)
        if hourly:
            cache.delete(hour_key)
        if daily:
            cache.delete(day_key)

    def generate_challenge(self) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        """
        Generates a random 6-digit OTP and sends it via email.

        Returns:
            str: The generated OTP code (for testing purposes)

        Raises:
            OTPThrottledError: If issuance is rate-limited (audit §1.3).
        """
        self._check_throttle()

        # Generate a cryptographically secure random 6-digit code
        code = str(secrets.randbelow(900000) + 100000)

        # Store hashed version
        from django.contrib.auth.hashers import make_password

        self.token = make_password(code)
        self.generated_at = timezone.now()
        self.failed_attempts = 0
        self.save()

        # Send email
        self._send_otp_email(code)

        # Only record the issuance after successful save + send. If the email
        # backend raises, the user can retry without burning a budget slot.
        self._record_issuance()

        return code

    def _send_otp_email(self, code: str) -> None:
        """Send the OTP code via email."""
        subject = "Your Two-Factor Authentication Code"
        message = f"""
Hello {self.user.username},

Your two-factor authentication code is: {code}

This code will expire in {self.valid_minutes} minutes.

If you did not request this code, please ignore this email and secure your account.

Best regards,
Security Team
        """

        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@example.com")
        recipient_list = [self.user.email]

        send_mail(
            subject,
            message,
            from_email,
            recipient_list,
            fail_silently=False,
        )

    def verify_token(self, token: str) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
        """
        Verify the provided OTP token.

        Args:
            token (str): The OTP code to verify

        Returns:
            bool: True if the token is valid, False otherwise
        """
        # Check if token exists
        if not self.token or not self.generated_at:
            return False

        # Check if too many failed attempts
        if self.failed_attempts >= self.max_attempts:
            return False

        # Check if token has expired
        expiry_time = self.generated_at + timedelta(minutes=self.valid_minutes)
        if timezone.now() > expiry_time:
            return False

        # Verify the token
        from django.contrib.auth.hashers import check_password

        is_valid = check_password(token, self.token)

        if is_valid:
            # Clear the token after successful verification
            self.token = ""
            self.generated_at = None
            self.failed_attempts = 0
            self.save()
            return True
        else:
            # Increment failed attempts
            self.failed_attempts += 1
            self.save()
            return False

    def is_interactive(self):
        """Returns True since this device requires user interaction."""
        return True

    def verify_is_allowed(self):
        """Check if verification is allowed (not too many failed attempts)."""
        return self.failed_attempts < self.max_attempts
