"""UK BACS sort code + account number validation.

Phase 3 ships **format-only** validation: digits-only, sort code is 6
digits, account numbers are 6-8 digits and left-padded to 8 (per BACS
convention; some legacy banks issue 6- or 7-digit numbers that BACS
expects with leading zeros). The full VocaLink modulus check (a ~500-row
table of bank-specific weights + modulus-10/-11 algorithms) is *not*
implemented yet — the validator returns
``(False, "format_only_check_passed")`` warnings the caller can promote
to a flag without blocking the donation. Adding the modulus table later
is a drop-in extension that doesn't touch any caller.

This module is deliberately UK-centric. Non-UK direct-debit donations
should not reach this validator.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------
SORT_CODE_LENGTH = 6
ACCOUNT_NUMBER_MIN_LENGTH = 6
ACCOUNT_NUMBER_MAX_LENGTH = 10  # building society + roll-number padding edge-case
ACCOUNT_NUMBER_CANONICAL_LENGTH = 8

REASON_INVALID_FORMAT = "invalid_format"
REASON_PASSED_FORMAT_ONLY = "format_only_check_passed"
REASON_OUTSIDE_PUBLISHED_TABLE = "sort_code_outside_published_ranges"

_NON_DIGIT = re.compile(r"[^0-9]")


def normalize_sort_code(raw: str | None) -> str:
    """Strip non-digit characters from a sort code.

    Returns the canonical 6-digit string (e.g. ``"60-83-71"`` →
    ``"608371"``). Returns the empty string on ``None``/empty input so
    the caller can short-circuit before calling :func:`validate_bacs`.
    Output longer or shorter than 6 digits is a validation error
    surfaced by :func:`validate_bacs`, not a normalisation error.
    """
    if not raw:
        return ""
    return _NON_DIGIT.sub("", raw)


def normalize_account_number(raw: str | None) -> str:
    """Strip non-digit characters and left-pad to 8 digits when shorter.

    Some legacy banks issue 6- or 7-digit account numbers; BACS expects
    them left-padded with zeros to 8 characters before submission.
    Numbers already at 8+ digits are returned unchanged.

    Returns the empty string on ``None``/empty input.
    """
    if not raw:
        return ""
    digits = _NON_DIGIT.sub("", raw)
    if not digits:
        return ""
    if len(digits) < ACCOUNT_NUMBER_CANONICAL_LENGTH:
        return digits.rjust(ACCOUNT_NUMBER_CANONICAL_LENGTH, "0")
    return digits


def validate_bacs(
    sort_code: str | None, account_number: str | None
) -> tuple[bool, str]:
    """Validate a UK BACS sort-code + account-number pair.

    Phase 3 ships format-only validation. The return contract is::

        (True,  "")                                  format ok
        (False, REASON_INVALID_FORMAT)               digits / length wrong
        (False, REASON_PASSED_FORMAT_ONLY)           format ok but no
                                                     modulus check ran
                                                     (treat as warning)

    A future patch can swap the format-only success path for the real
    VocaLink modulus check without changing any caller.

    Args:
        sort_code: Raw operator-typed sort code, with or without
            separators (``"60-83-71"``, ``"60 83 71"``, ``"608371"``).
        account_number: Raw operator-typed account number.

    Returns:
        Tuple of ``(passed, reason)`` where ``passed`` is True only for
        a clean modulus check (currently unreachable until the table is
        shipped) and ``reason`` is one of the public REASON_* constants.
    """
    sc = normalize_sort_code(sort_code)
    an = normalize_account_number(account_number)

    if len(sc) != SORT_CODE_LENGTH:
        return False, REASON_INVALID_FORMAT
    if not (ACCOUNT_NUMBER_MIN_LENGTH <= len(an) <= ACCOUNT_NUMBER_MAX_LENGTH):
        return False, REASON_INVALID_FORMAT

    # Format ok. The modulus check is not yet shipped — flag for QA so
    # operators don't silently pass invalid combos. The caller is free
    # to suppress this when the operator force-saves.
    return False, REASON_PASSED_FORMAT_ONLY
