"""Free UK postcode lookup via postcodes.io.

No API key required. Results are cached in-process with :func:`functools.lru_cache`
to avoid repeated network calls for the same postcode within a worker lifetime.

API reference: https://postcodes.io
"""

import json
import logging
import urllib.parse
from functools import lru_cache
from http.client import HTTPSConnection

logger = logging.getLogger(__name__)

_API_HOST = "api.postcodes.io"
_API_PATH_PREFIX = "/postcodes"
_TIMEOUT_SECS = 3  # fail fast rather than block the scan pipeline


@lru_cache(maxsize=512)
def lookup_postcode(postcode: str) -> dict[str, str] | None:
    """Validate a UK postcode and return city/county enrichment data.

    Calls postcodes.io (free, no API key). Results are in-process cached.

    Args:
        postcode: UK postcode in any format — spaces and case are normalised.

    Returns:
        - Populated dict with ``city`` and ``county`` — postcode is valid.
        - Empty dict ``{}`` — postcode confirmed invalid (HTTP 404).
        - ``None`` — network/API error; validity is unknown, keep postcode.

    Examples:
        >>> lookup_postcode("SW1A 1AA")
        {'city': 'City Of Westminster', 'county': 'London'}
        >>> lookup_postcode("ZZ99 9ZZ")
        {}
    """
    normalised = postcode.upper().replace(" ", "")
    request_path = f"{_API_PATH_PREFIX}/{urllib.parse.quote(normalised)}"
    connection = HTTPSConnection(_API_HOST, timeout=_TIMEOUT_SECS)
    try:
        connection.request("GET", request_path)
        with connection.getresponse() as resp:
            if resp.status == 404:
                logger.debug("postcodes.io: postcode not found: %r", postcode)
                return {}  # confirmed invalid
            if resp.status != 200:
                logger.warning(
                    "postcodes.io HTTP %s for postcode %r",
                    resp.status,
                    postcode,
                )
                return None  # unknown — don't clear the postcode
            data: dict = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("postcodes.io lookup failed for %r: %s", postcode, exc)
        return None  # unknown — don't clear the postcode
    finally:
        connection.close()

    api_result: dict = data.get("result") or {}
    city: str = api_result.get("admin_district") or ""
    # admin_county is empty for London boroughs — fall back to region
    county: str = api_result.get("admin_county") or api_result.get("region") or ""
    return {
        "city": city.title() if city else "",
        "county": county.title() if county else "",
    }
