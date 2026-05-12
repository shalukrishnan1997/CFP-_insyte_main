"""UK phone number normalisation for donor search and matching.

The same human phone number is written in many forms:

* ``"07700 900 123"`` — common spaced form on house-file imports
* ``"07700900123"`` — digit-only form often produced by OCR
* ``"+44 7700 900 123"`` — international form when donor self-supplies
* ``"(0)7700 900 123"`` — parenthesised optional-zero form
* ``"0044-7700-900-123"`` — hyphenated alternate international form

Direct equality / ``__icontains`` queries fail across these variants. This
module normalises to a single canonical representation: digits-only with
the UK ``+44`` / ``0044`` international prefix collapsed to a national
``0``. The result is suitable for storing in an indexed ``normalized_phone``
column and for direct ``__icontains`` queries.

Used by:

* :class:`donors.models.SystemDonor` — populates ``normalized_phone`` on
  ``save()`` so phone-based donor search hits an index.
* :func:`custom_admin.api_views._search_system_donors_by_phone` — normalises
  the operator-supplied query before querying ``normalized_phone``.

The helper is deliberately UK-focused (the product targets UK charity
service bureaus). Donors with non-UK numbers are stored verbatim minus
formatting characters; international country codes other than ``+44`` are
preserved as digits.
"""

from __future__ import annotations

import re

# Anything that is NOT a digit or "+" gets stripped.
_NON_PHONE_CHARS = re.compile(r"[^0-9+]")


def normalize_phone(value: str | None) -> str:
    """Return a canonical, comparable phone string.

    Strips whitespace, hyphens, parentheses, slashes, dots and other
    formatting punctuation. Collapses UK international prefixes
    (``+44``, ``0044``) to the national ``0`` form. Leaves any other
    international prefix as digits without the ``+`` sign.

    Returns the empty string when *value* is ``None`` or contains no
    digits, so callers can use the result directly in
    ``Q(normalized_phone__icontains=...)`` without further checks.

    Args:
        value: Raw phone string from a form, OCR field or import row.

    Returns:
        Normalised digits-only phone string.

    Examples:
        >>> normalize_phone("07700 900 123")
        '07700900123'
        >>> normalize_phone("+44 7700 900 123")
        '07700900123'
        >>> normalize_phone("(0044) 7700-900-123")
        '07700900123'
        >>> normalize_phone("020 7946 0958")
        '02079460958'
        >>> normalize_phone("")
        ''
        >>> normalize_phone(None)
        ''
    """
    if not value:
        return ""

    # Strip everything that isn't a digit or a "+". The "+" survives
    # only long enough to recognise UK international prefixes.
    cleaned = _NON_PHONE_CHARS.sub("", value)
    if not cleaned:
        return ""

    if cleaned.startswith("+44"):
        return "0" + cleaned[3:]
    if cleaned.startswith("0044"):
        return "0" + cleaned[4:]
    if cleaned.startswith("+"):
        # Preserve non-UK international numbers as digits-only.
        return cleaned[1:]
    return cleaned


def looks_like_phone_query(value: str | None) -> bool:
    """Return ``True`` when *value* should branch into the phone-search path.

    A phone-shaped query is one that, after normalisation, contains at
    least 6 digits. Below that threshold, a digit-string is more likely
    a URN suffix than a phone number, and the standard text-search path
    yields better results.

    Args:
        value: Raw search query from the operator.

    Returns:
        True when the normalised value has 6 or more digits.
    """
    return len(normalize_phone(value)) >= 6
