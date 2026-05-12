"""OCR field utility functions.

Only the functions actively used by the rest of the OCR package are kept here.

Used by entity_overrides.py:
    parse_address_block, address_looks_noisy

Used by donor_matching.py:
    find_urn_in_text, extract_urn_by_pattern
"""

import logging
import re

from . import patterns as P

logger = logging.getLogger(__name__)


# ─── URN ──────────────────────────────────────────────────────────────────────


def find_urn_in_text(text: str, known_urns: list[str]) -> tuple[str, float]:
    """Search for a known URN in OCR text using exact and word-boundary matching.

    Args:
        text: Full OCR text.
        known_urns: List of valid URNs to match against.

    Returns:
        Tuple of (matched_urn, confidence), or (``""``, ``0.0``) if no match.
    """
    text_upper = text.upper()

    for urn in known_urns:
        if urn and urn.upper() in text_upper:
            return urn, 0.98

    words_upper = {w.upper() for w in re.findall(r"[\w\-]+", text)}
    urn_set = {u.upper(): u for u in known_urns}
    for word_upper in words_upper:
        if word_upper in urn_set:
            return urn_set[word_upper], 0.95

    return "", 0.0


def extract_urn_by_pattern(text: str) -> tuple[str, float]:
    """Extract a URN using common alphanumeric patterns (fallback when no data file).

    Args:
        text: Full OCR text.

    Returns:
        Tuple of (urn_candidate, confidence).
    """
    fallback_patterns = [
        re.compile(r"URN[:\s]+([A-Z0-9\-]{3,20})", re.IGNORECASE),
        re.compile(r"Ref(?:erence)?[:\s]+([A-Z0-9\-]{3,20})", re.IGNORECASE),
        re.compile(r"\b([A-Z]{2,10}[\-][A-Z0-9\-]{2,15})\b"),
    ]
    for pattern in fallback_patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip(), 0.6
    return "", 0.0


# ─── Address ──────────────────────────────────────────────────────────────────


def parse_address_block(text: str) -> dict[str, str]:
    """Parse a multi-line UK address block into structured components.

    Handles common formats::

        26 Briars Wood
        HATFIELD AL10 8DG

        Flat 2
        12 High Street
        LONDON SW1A 1AA

    Args:
        text: Raw address string, possibly multi-line.

    Returns:
        Dict with any of: ``address_line1``, ``address_line2``, ``city``,
        ``postcode``.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return {}

    postcode = ""
    city = ""
    addr_lines: list[str] = []

    last_with_pc = -1
    for i in range(len(lines) - 1, -1, -1):
        pc_match = P.POSTCODE_PATTERN.search(lines[i])
        if pc_match:
            raw_pc = pc_match.group(1).upper().replace(" ", "")
            postcode = f"{raw_pc[:-3]} {raw_pc[-3:]}"
            city_part = lines[i][: pc_match.start()].strip().rstrip(",").strip()
            if city_part:
                city = city_part.title()
            last_with_pc = i
            break

    addr_lines = lines[:last_with_pc] if last_with_pc >= 0 else lines

    result: dict[str, str] = {}
    if addr_lines:
        result["address_line1"] = addr_lines[0]
    if len(addr_lines) >= 2:
        result["address_line2"] = ", ".join(addr_lines[1:])
    if city:
        result["city"] = city
    if postcode:
        result["postcode"] = postcode
    return result


def address_looks_noisy(parsed: dict[str, str]) -> bool:
    """Determine whether a parsed address block looks like contact-label noise.

    Form Parser sometimes returns a broad "Address" field that contains labels
    such as "Mobile", "Email", "Number" instead of a postal address.

    Args:
        parsed: Parsed address components from :func:`parse_address_block`.

    Returns:
        ``True`` when the components look like non-address label text.
    """
    if not parsed:
        return True

    line1 = str(parsed.get("address_line1", "") or "").strip()
    line2 = str(parsed.get("address_line2", "") or "").strip()
    city = str(parsed.get("city", "") or "").strip()
    postcode = str(parsed.get("postcode", "") or "").strip()

    combined = " ".join(part for part in (line1, line2, city) if part).lower()
    if not combined and not postcode:
        return True

    if line1.lower() in {"address", "mobile", "phone", "email", "number"}:
        return True

    noise_hits = sum(1 for kw in P.ADDRESS_NOISE_KEYWORDS if kw in combined)
    has_street_hint = bool(
        P.STREET_SUFFIX_RE.search(combined) or re.search(r"^\d+[A-Za-z]?\s+\w+", line1)
    )
    contains_email_like = any(
        marker in combined for marker in ("@", ".com", ".co.uk", "hotmail")
    )

    if contains_email_like and not postcode:
        return True

    return noise_hits >= 2 and not postcode and not has_street_hint
