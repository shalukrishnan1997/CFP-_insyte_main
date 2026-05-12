"""UK postcode validation and address enrichment via postcodes.io.

``postcodes.io`` is a **free, no-API-key, MIT-licensed** service backed by
official Ordnance Survey and ONS data.  GitHub: ideal-postcodes/postcodes.io.

Usage in extraction pipeline::

    from core.services.postcode import PostcodesIOService

    # After OCR extraction — fills city/county from authoritative OS data
    PostcodesIOService.enrich_address(result)

What this does
--------------
1. **Validates** the extracted postcode against the OS / Royal Mail dataset.
2. **Enriches city** if blank — uses ``parish`` (most specific locality) and
   falls back to ``admin_district`` (works for Scotland where parish is null).
3. **Enriches county** if blank — uses ``admin_county`` (null for Scotland,
   where only ``admin_district`` is available).
4. Sets ``result["postcode_valid"]`` to ``True`` / ``False``.

Fields returned by postcodes.io that we use
--------------------------------------------
* ``parish``        — most specific civil locality (e.g. "Hatfield", "Bury St Edmunds")
* ``admin_district``— local authority district (e.g. "Welwyn Hatfield", "Glasgow City")
* ``admin_county``  — county (e.g. "Hertfordshire", "Suffolk") — null in Scotland
* ``country``       — "England" / "Scotland" / "Wales" / "Northern Ireland"

Rate limits / self-hosting
--------------------------
The public API has no enforced rate limit, but is subject to fair use.
For high-volume production use, self-hosting the open-source server is trivial
(Docker image available).  See postcodes.io/docs/self-host.

Results are cached locally per process run using ``functools.lru_cache`` to
avoid redundant round-trips for the same postcode within a scan batch.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Canonical UK postcode regex — matches both spaced and unspaced forms
_PC_RE = re.compile(
    r"^([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})$",
    re.IGNORECASE,
)

_BASE_URL = "https://api.postcodes.io"
_TIMEOUT = 5.0  # seconds


def _normalise_postcode(postcode: str) -> str:
    """Normalise to the canonical spaced form ``SW1A 1AA``.

    Args:
        postcode: Raw postcode string (may be unspaced or mixed case).

    Returns:
        Normalised postcode (e.g. ``"AL10 8DG"``), or original if malformed.
    """
    pc = postcode.strip().upper().replace(" ", "")
    if len(pc) >= 5:
        # Inward code is always 3 chars
        return f"{pc[:-3]} {pc[-3:]}"
    return postcode.strip().upper()


def _is_valid_format(postcode: str) -> bool:
    """Return True if the postcode has a structurally valid UK format.

    Args:
        postcode: Postcode string (spaces allowed).

    Returns:
        True if it matches the UK postcode pattern.
    """
    return bool(_PC_RE.match(postcode.strip()))


@lru_cache(maxsize=512)
def _lookup_postcode(normalised: str) -> dict[str, Any] | None:
    """Call postcodes.io and return the result dict, or None on any failure.

    The response is cached per normalised postcode so repeated calls within
    the same Django process do not make duplicate HTTP requests.

    Args:
        normalised: Uppercase spaced postcode (e.g. ``"AL10 8DG"``).

    Returns:
        The ``"result"`` sub-dict from the postcodes.io JSON response, or None.
    """
    url = f"{_BASE_URL}/postcodes/{normalised.replace(' ', '')}"
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("result")
        if resp.status_code == 404:
            logger.debug("postcodes.io: postcode not found: %r", normalised)
        else:
            logger.warning(
                "postcodes.io: unexpected status %d for %r",
                resp.status_code,
                normalised,
            )
    except requests.RequestException as exc:
        logger.warning("postcodes.io: request failed for %r: %s", normalised, exc)
    return None


class PostcodesIOService:
    """Address validation and enrichment using the free postcodes.io API.

    All public methods are static — no instance required.

    Example::

        result = OCRExtractor.extract_from_document_ai(...)
        PostcodesIOService.enrich_address(result)
        # result["postcode_valid"] is now True/False
        # result["city"] is populated from parish/admin_district if blank
        # result["county"] is populated from admin_county if blank
    """

    @staticmethod
    def enrich_address(result: dict[str, Any]) -> None:
        """Validate postcode and back-fill city/county from postcodes.io.

        Mutates ``result`` in place.  Safe to call even when postcode is empty
        (becomes a no-op).  Never overwrites a city or county that was already
        extracted by OCR.

        Args:
            result: Extraction result dict (mutated in place).
        """
        postcode_raw = str(result.get("postcode", "") or "").strip()
        if not postcode_raw:
            return

        if not _is_valid_format(postcode_raw):
            logger.debug(
                "postcodes.io: invalid postcode format %r — skipping lookup",
                postcode_raw,
            )
            result["postcode_valid"] = False
            return

        normalised = _normalise_postcode(postcode_raw)
        # Persist the normalised form back into the result
        result["postcode"] = normalised

        data = _lookup_postcode(normalised)
        if data is None:
            result["postcode_valid"] = False
            return

        result["postcode_valid"] = True

        # ── Enrich city ──────────────────────────────────────────────────────
        # Use the most specific locality available:
        #   parish (England/Wales civil parish) > admin_district (Scotland + all)
        if not result.get("city"):
            city = data.get("parish") or data.get("admin_district") or ""
            if city:
                result["city"] = city
                result["city_confidence"] = 0.95
                logger.debug(
                    "postcodes.io: enriched city=%r for postcode=%r", city, normalised
                )

        # ── Enrich county ────────────────────────────────────────────────────
        # admin_county is null in Scotland; fall back to admin_district there.
        if not result.get("county"):
            county = (
                data.get("admin_county")
                or (
                    data.get("admin_district")
                    if data.get("country") == "Scotland"
                    else ""
                )
                or ""
            )
            if county:
                result["county"] = county
                result["county_confidence"] = 0.95
                logger.debug(
                    "postcodes.io: enriched county=%r for postcode=%r",
                    county,
                    normalised,
                )
