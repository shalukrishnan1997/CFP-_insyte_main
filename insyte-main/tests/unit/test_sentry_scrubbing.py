"""Unit tests for ``responsehandling.sentry_scrubber.strip_pii``.

Audit 2026-05-02 §1.2: the prior ``_strip_pii`` covered only password / OTP
keys at ``event['request']['data']``. The new handler walks the request
envelope, redacts any deny-listed key in the request body / query / env,
and strips cookies / sensitive headers wholesale.

The wider event tree (``extra``, ``user``, ``breadcrumbs``, frames, spans)
is handled by Sentry's built-in ``EventScrubber`` configured in
``responsehandling.settings.production``. ``event["contexts"]`` is
intentionally untouched here — see ``TestFrameworkContextsUntouched``.
"""

from __future__ import annotations

from typing import Any

import pytest

from responsehandling.sentry_scrubber import (
    REDACTED_VALUE,
    SENSITIVE_KEYS,
    strip_pii,
)


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"event_id": "abc"}
    base.update(overrides)
    return base


class TestStripPiiBasics:
    def test_password_in_request_data_is_redacted(self) -> None:
        event = _event(request={"data": {"username": "alice", "password": "hunter2"}})
        out = strip_pii(event, {})
        assert out["request"]["data"]["password"] == REDACTED_VALUE
        assert out["request"]["data"]["username"] == "alice"  # untouched

    def test_otp_token_is_redacted(self) -> None:
        event = _event(request={"data": {"otp_token": "123456"}})
        assert strip_pii(event, {})["request"]["data"]["otp_token"] == REDACTED_VALUE

    def test_event_with_no_request_passes_through(self) -> None:
        event = _event()
        out = strip_pii(event, {})
        assert out == {"event_id": "abc"}

    def test_handler_returns_same_object(self) -> None:
        """In-place mutation contract — Sentry expects the returned event."""
        event = _event(request={"data": {"password": "x"}})
        assert strip_pii(event, {}) is event


class TestBankFieldRedaction:
    """Bank fields are encrypted at rest; they must also be redacted at egress."""

    @pytest.mark.parametrize(
        "field",
        ["sort_code", "account_number", "iban", "bic"],
    )
    def test_bank_fields_redacted_in_request_data(self, field: str) -> None:
        event = _event(request={"data": {field: "20-30-40"}})
        assert strip_pii(event, {})["request"]["data"][field] == REDACTED_VALUE


class TestDonorPiiRedaction:
    @pytest.mark.parametrize(
        "field",
        [
            "first_name",
            "last_name",
            "email",
            "phone",
            "postcode",
            "address",
            "dob",
            "urn",
        ],
    )
    def test_donor_pii_redacted_in_request_data(self, field: str) -> None:
        event = _event(request={"data": {field: "value-of-pii"}})
        assert strip_pii(event, {})["request"]["data"][field] == REDACTED_VALUE


class TestNestedRedaction:
    def test_nested_dict_is_walked(self) -> None:
        event = _event(
            request={
                "data": {
                    "donor": {
                        "first_name": "Jane",
                        "address": "1 High St",
                        "metadata": {"email": "jane@example.com"},
                    }
                }
            }
        )
        out = strip_pii(event, {})
        donor = out["request"]["data"]["donor"]
        assert donor["first_name"] == REDACTED_VALUE
        assert donor["address"] == REDACTED_VALUE
        assert donor["metadata"]["email"] == REDACTED_VALUE

    def test_lists_inside_nested_dicts_are_walked(self) -> None:
        event = _event(
            request={
                "data": {
                    "donations": [
                        {"sort_code": "20-30-40"},
                        {"sort_code": "11-22-33"},
                    ]
                }
            }
        )
        out = strip_pii(event, {})
        for donation in out["request"]["data"]["donations"]:
            assert donation["sort_code"] == REDACTED_VALUE


class TestRequestEnvelopeStripping:
    def test_cookies_block_is_replaced_wholesale(self) -> None:
        event = _event(request={"cookies": {"sessionid": "xyz", "csrftoken": "abc"}})
        out = strip_pii(event, {})
        assert out["request"]["cookies"] == REDACTED_VALUE

    def test_authorization_header_is_redacted(self) -> None:
        event = _event(
            request={"headers": {"Authorization": "Bearer abcdef", "Accept": "*/*"}}
        )
        out = strip_pii(event, {})
        assert out["request"]["headers"]["Authorization"] == REDACTED_VALUE
        assert out["request"]["headers"]["Accept"] == "*/*"

    def test_stripe_signature_header_is_redacted(self) -> None:
        event = _event(request={"headers": {"X-Stripe-Signature": "t=1,v1=abc"}})
        out = strip_pii(event, {})
        assert out["request"]["headers"]["X-Stripe-Signature"] == REDACTED_VALUE

    def test_scan_signature_header_is_redacted(self) -> None:
        event = _event(request={"headers": {"X-Scan-Signature": "abcdef"}})
        out = strip_pii(event, {})
        assert out["request"]["headers"]["X-Scan-Signature"] == REDACTED_VALUE


class TestFrameworkContextsUntouched:
    """``event["contexts"]`` and other non-request sections are NOT walked.

    Sentry's SDK populates ``contexts.runtime.name``, ``contexts.os.name``,
    ``contexts.device.name``, etc. with framework metadata that's essential
    for debugging. A previous version of the scrubber recursed into the
    whole event tree with ``"name"`` in the deny-list, redacting all of
    those values. The fix narrows the walk to ``request.*`` only and
    relies on Sentry's built-in ``EventScrubber`` for ``extra`` /
    ``breadcrumbs`` / etc. (handled outside this unit test surface).
    """

    @pytest.mark.parametrize(
        "section",
        ["runtime", "os", "device", "browser", "app"],
    )
    def test_framework_context_name_survives(self, section: str) -> None:
        event = _event(contexts={section: {"name": "Framework", "version": "1.0"}})
        out = strip_pii(event, {})
        assert out["contexts"][section]["name"] == "Framework"
        assert out["contexts"][section]["version"] == "1.0"

    def test_extra_section_is_not_walked_here(self) -> None:
        """``extra`` is EventScrubber's job; ``strip_pii`` leaves it alone."""
        event = _event(extra={"webhook_secret": "whsec_abc"})
        assert strip_pii(event, {})["extra"]["webhook_secret"] == "whsec_abc"

    def test_breadcrumbs_are_not_walked_here(self) -> None:
        """Breadcrumbs are EventScrubber's job; ``strip_pii`` leaves them alone."""
        event = _event(breadcrumbs=[{"data": {"password": "x"}}])
        assert strip_pii(event, {})["breadcrumbs"][0]["data"]["password"] == "x"


class TestDenylistInvariants:
    def test_denylist_contains_known_critical_keys(self) -> None:
        """Spot-check: anything we definitely want to redact should be present."""
        for k in {
            "sort_code",
            "account_number",
            "webhook_secret",
            "field_encryption_salt",
            "first_name",
            "email",
        }:
            assert k in SENSITIVE_KEYS, k

    def test_bare_name_is_not_in_denylist(self) -> None:
        """Bare ``name`` would over-match framework metadata (runtime/os/etc.)
        when EventScrubber applies the same deny-list to ``extra`` /
        ``breadcrumbs``. Donor names are covered by ``first_name`` /
        ``last_name`` / ``full_name``."""
        assert "name" not in SENSITIVE_KEYS

    def test_case_insensitive_match(self) -> None:
        """A header / form key with mixed casing still gets redacted."""
        event = _event(request={"data": {"Sort_Code": "20-30-40"}})
        assert strip_pii(event, {})["request"]["data"]["Sort_Code"] == REDACTED_VALUE
