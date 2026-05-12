"""PII scrubber for Sentry ``before_send`` events.

Sentry receives an event whenever an exception or message is captured. The
event payload commonly contains request bodies, breadcrumb data, and
exception arguments — any of which can pick up PII that the application
otherwise keeps encrypted at rest.

Scope: ``strip_pii`` only walks the ``event["request"]`` envelope. The wider
event tree (``extra``, ``user``, ``breadcrumbs``, frames, spans) is covered
by Sentry's built-in ``EventScrubber``, wired up in
``responsehandling.settings.production`` with the same ``SENSITIVE_KEYS``
deny-list. ``event["contexts"]`` is deliberately *not* scrubbed: the
SDK populates it with framework metadata (``contexts.runtime.name``,
``contexts.os.name``, ``contexts.device.name``, ...), and a generic
deny-list key like ``"name"`` would redact those values and degrade
debuggability of every event.

The handler is split out (rather than inlined in ``production.py``) so it
can be unit-tested without importing the full production settings module.
"""

from __future__ import annotations

from typing import Any

# ─── Deny-list (audit 2026-05-02 §1.2) ─────────────────────────────────
# Keys whose *values* must never reach Sentry. Match is case-insensitive on
# the exact key name as it appears in any nested dict.
#
# Categories:
#   * Authentication: passwords, OTPs, session/csrf tokens.
#   * Bank / payment: sort code, account number, IBAN, BIC, raw card data.
#   * Donor / personal: first/last/full name, email, phone, postcode,
#     full address, DOB, URN (the donor reference number used by every
#     charity). Bare "name" is intentionally NOT in the deny-list — too
#     many framework / library payloads use a generic "name" key.
#   * Encryption / signing: anything that lets an attacker forge HMAC or
#     decrypt at-rest data.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        # Authentication
        "password",
        "new_password",
        "confirm_password",
        "current_password",
        "password1",
        "password2",
        "old_password",
        "otp_token",
        "otp",
        "totp",
        "csrfmiddlewaretoken",
        "session_id",
        "sessionid",
        # Card / payment
        "credit_card",
        "card_number",
        "cardnumber",
        "cvv",
        "cvc",
        "card_cvc",
        "card_cvv",
        "exp_month",
        "exp_year",
        "card_exp_month",
        "card_exp_year",
        # Bank
        "sort_code",
        "sortcode",
        "account_number",
        "accountnumber",
        "iban",
        "bic",
        "swift",
        # Donor PII
        "first_name",
        "last_name",
        "full_name",
        "email",
        "email_address",
        "phone",
        "phone_number",
        "mobile",
        "postcode",
        "post_code",
        "zip",
        "zipcode",
        "address",
        "address_line_1",
        "address_line_2",
        "dob",
        "date_of_birth",
        "urn",
        # Webhook / API secrets
        "webhook_secret",
        "secret_key",
        "api_key",
        "stripe_secret_key",
        "stripe_webhook_secret",
        "field_encryption_salt",
        "scan_webhook_secret",
    }
)

REDACTED_VALUE = "[REDACTED]"


def _redact_in_place(obj: Any) -> None:
    """Walk ``obj`` recursively and replace any value at a sensitive key.

    Operates in place. Lists are descended; dicts have their values either
    redacted (if the key matches) or recursed into. Non-container values
    are left untouched.
    """
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            lowered = key.lower() if isinstance(key, str) else ""
            if lowered in SENSITIVE_KEYS:
                obj[key] = REDACTED_VALUE
            else:
                _redact_in_place(obj[key])
    elif isinstance(obj, list):
        for item in obj:
            _redact_in_place(item)


def strip_pii(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Sentry ``before_send`` handler — strip PII from the request envelope.

    Walks ``event["request"]`` only. Handles the request-specific surfaces
    that Sentry's built-in ``EventScrubber`` doesn't cover at the same
    granularity (cookies wholesale, sensitive headers exact-match), then
    recurses through the request body / query / env.

    Sentry's ``EventScrubber`` (configured in ``settings.production``)
    handles ``extra``, ``user``, ``breadcrumbs``, frames, and spans with
    the same ``SENSITIVE_KEYS`` deny-list. ``event["contexts"]`` is
    intentionally untouched so framework metadata reaches Sentry intact.

    Args:
        event: Sentry event dict (mutated in place).
        _hint: Sentry-supplied hint dict; unused here.

    Returns:
        The same event, with sensitive keys redacted under ``request``.
    """
    request = event.get("request")
    if isinstance(request, dict):
        # Cookies frequently carry session ids — strip the whole bag rather
        # than enumerate which cookie names are sensitive.
        if "cookies" in request:
            request["cookies"] = REDACTED_VALUE
        # Headers can contain Authorization / Cookie / X-Stripe-Signature.
        if "headers" in request:
            headers = request["headers"]
            if isinstance(headers, dict):
                for header in list(headers.keys()):
                    if header.lower() in {
                        "authorization",
                        "cookie",
                        "x-csrftoken",
                        "x-stripe-signature",
                        "x-scan-signature",
                    }:
                        headers[header] = REDACTED_VALUE
        # Recurse into the request body / query / env.
        _redact_in_place(request)

    return event
