"""Tests for the OTP issuance throttle (audit 2026-05-02 §1.3).

Three fixed-TTL windows guard against an attacker who has stolen a
password trying to brute-force the 6-digit OTP by repeatedly requesting
fresh codes:

* 60-second minimum interval between issuances
* 3 issuances per fixed hour window
* 10 issuances per fixed 24-hour window

The test suite asserts each window blocks the next call and that the
``OTPThrottledError`` signals the *type* of window without leaking the
remaining cool-down (which would let an attacker enumerate it).
"""

from __future__ import annotations

import pytest
from django.core.cache import cache

from auth_app.models import (
    _OTP_DAILY_LIMIT,
    _OTP_HOURLY_LIMIT,
    EmailDevice,
    OTPThrottledError,
)
from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def clear_cache():
    """LocMemCache test backend persists across tests in a process. Reset."""
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db()
class TestOtpIssuanceThrottle:
    def _make_device(self) -> EmailDevice:
        user = UserFactory()
        return EmailDevice.objects.create(
            user=user,
            name="email-otp",
            confirmed=True,
        )

    def test_first_issuance_succeeds(self) -> None:
        device = self._make_device()
        code = device.generate_challenge()
        assert len(code) == 6
        assert code.isdigit()

    def test_second_issuance_within_60s_is_throttled(self) -> None:
        device = self._make_device()
        device.generate_challenge()
        with pytest.raises(OTPThrottledError) as exc:
            device.generate_challenge()
        assert "min_interval" in str(exc.value)

    def test_throttle_clears_after_min_interval(self) -> None:
        """Manually clear the 60s window to simulate it expiring."""
        device = self._make_device()
        device.generate_challenge()
        device._reset_throttle(min_interval=True)
        # Should be allowed again (still within the hourly budget).
        device.generate_challenge()

    def test_hourly_limit_blocks_fourth_issuance(self) -> None:
        device = self._make_device()

        for _ in range(_OTP_HOURLY_LIMIT):
            device._reset_throttle(min_interval=True)  # bypass 60s gate
            device.generate_challenge()

        device._reset_throttle(min_interval=True)
        with pytest.raises(OTPThrottledError) as exc:
            device.generate_challenge()
        assert "hourly_limit" in str(exc.value)

    def test_daily_limit_blocks_eleventh_issuance(self) -> None:
        device = self._make_device()

        # Bypass the 60s gate AND simulate hour-window roll-over before each
        # issuance so the per-hour gate doesn't block first; we want to
        # exercise the day gate.
        for _ in range(_OTP_DAILY_LIMIT):
            device._reset_throttle(min_interval=True, hourly=True)
            device.generate_challenge()

        device._reset_throttle(min_interval=True, hourly=True)
        with pytest.raises(OTPThrottledError) as exc:
            device.generate_challenge()
        assert "daily_limit" in str(exc.value)

    def test_throttle_is_per_user(self) -> None:
        """Two users must not share a throttle bucket."""
        device_a = self._make_device()
        device_b = self._make_device()
        device_a.generate_challenge()
        # B's first issuance must succeed even though A's is in the 60s window.
        device_b.generate_challenge()

    def test_email_send_failure_does_not_consume_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If email send raises, budget counters must not advance."""
        device = self._make_device()

        def boom(self_inner: object, _code: str) -> None:
            raise RuntimeError("smtp down")

        monkeypatch.setattr(EmailDevice, "_send_otp_email", boom)

        with pytest.raises(RuntimeError):
            device.generate_challenge()

        # Counters were never bumped because ``_record_issuance`` runs after
        # ``_send_otp_email``.
        last_key, hour_key, day_key = device._throttle_keys()
        assert cache.get(last_key) is None
        assert cache.get(hour_key) is None
        assert cache.get(day_key) is None
