"""Donor matching and URN resolution for OCR-scanned donation forms.

Provides :func:`match_donor` which resolves an extracted URN to a
``Donor`` or ``DataFileDonor`` model instance.
"""

import logging
from typing import Any

from django.db import DatabaseError
from django.db.models import Q

from .field_extractors import extract_urn_by_pattern, find_urn_in_text

logger = logging.getLogger(__name__)


def _campaign_client_id(campaign: Any) -> str:
    """Return the campaign client identifier."""
    return str(campaign.client_id)


# ─── URN Loading ─────────────────────────────────────────────────────────────


def load_campaign_urns(campaign: Any) -> list[str]:
    """Load all URNs from the campaign's configured donor source.

    For ``data_file`` campaigns, URNs are loaded from both the data file and
    the house file and deduplicated.  This ensures OCR text-matching can find
    donors from either source — for example when the data file arrives after
    scanning has already started.

    For ``house_file`` campaigns only the house file is loaded.

    Args:
        campaign: Campaign model instance.

    Returns:
        List of URN strings (deduplicated).
    """
    from django.core.exceptions import ObjectDoesNotExist

    from donors.models import DataFileDonor, Donor

    donor_source = getattr(campaign, "donor_source", "house_file")

    try:
        data_file = campaign.data_file
    except AttributeError, ObjectDoesNotExist:
        data_file = None

    try:
        client_id = _campaign_client_id(campaign)
        if donor_source == "house_file":
            donor_queryset = Donor.objects.filter(client_id=client_id)
            return list(
                donor_queryset.exclude(urn="")
                .exclude(urn__isnull=True)
                .values_list("urn", flat=True)
                .iterator()
            )

        # data_file source: merge data file URNs + house file URNs so OCR
        # text-matching can find donors from either source (e.g. when the
        # data file arrives late).
        urns: set[str] = set()
        if data_file is not None:
            urns.update(
                DataFileDonor.objects.filter(data_file=data_file)
                .exclude(urn="")
                .exclude(urn__isnull=True)
                .values_list("urn", flat=True)
                .iterator()
            )
        else:
            logger.warning(
                "Campaign %s has no data file yet; loading URNs from house file only",
                getattr(campaign, "id", "unknown"),
            )

        donor_queryset = Donor.objects.filter(client_id=client_id)
        urns.update(
            donor_queryset.exclude(urn="")
            .exclude(urn__isnull=True)
            .values_list("urn", flat=True)
            .iterator()
        )
        return list(urns)
    except DatabaseError:
        logger.exception("Failed to load campaign URNs")
        return []


def extract_urn(
    text: str,
    campaign: Any,
    known_urns: list[str] | None = None,
) -> tuple[str, float]:
    """Extract URN by matching OCR text against known campaign URNs.

    Strategy:
    1. Load all valid URNs for the campaign's data file (or use pre-loaded list).
    2. Search the OCR text for any exact URN match.
    3. Fall back to pattern matching when no data file is available.

    Args:
        text: Full OCR text.
        campaign: Campaign model instance.
        known_urns: Pre-loaded URN list, or ``None`` to load from DB.

    Returns:
        Tuple of (matched_urn, confidence).
    """
    if known_urns is None:
        known_urns = load_campaign_urns(campaign)

    if not known_urns:
        return extract_urn_by_pattern(text)

    matched_urn, confidence = find_urn_in_text(text, known_urns)
    if matched_urn:
        return matched_urn, confidence

    logger.debug("No URN match found in OCR text for campaign %s", campaign.id)
    return "", 0.0


# ─── Donor Display Name ───────────────────────────────────────────────────────


def get_donor_display_name(donor: Any) -> str:
    """Get the display name for a donor or data file donor.

    Args:
        donor: Donor or DataFileDonor model instance.

    Returns:
        Full name string.
    """
    full_name = getattr(donor, "full_name", "")
    if full_name:
        return str(full_name)
    title = getattr(donor, "title", "")
    first = getattr(donor, "first_name", "")
    last = getattr(donor, "last_name", "")
    return f"{title} {first} {last}".strip()


# ─── Donor Matching ───────────────────────────────────────────────────────────


def match_data_file_donor(urn: str, data_file: Any) -> dict[str, Any] | None:
    """Try to match URN against the campaign's data file donors.

    Args:
        urn: URN to match.
        data_file: DataFile instance.

    Returns:
        Match result dict if found, or ``None``.
    """
    from donors.models import DataFileDonor

    df_donor = (
        DataFileDonor.objects.select_related("house_file_donor")
        .filter(data_file=data_file, urn__iexact=urn)
        .first()
    )
    if df_donor is None:
        return None

    return {
        "donor": df_donor.house_file_donor if df_donor.house_file_donor else None,
        "data_file_donor": df_donor,
        "source": "data_file",
        "donor_name": get_donor_display_name(df_donor),
    }


def match_house_file_donor(urn: str, campaign: Any) -> dict[str, Any] | None:
    """Try to match URN against the house file donors.

    Args:
        urn: URN to match.

    Returns:
        Match result dict if found, or ``None``.
    """
    from donors.models import Donor

    donor_queryset = Donor.objects.filter(Q(urn__iexact=urn))
    client_id = _campaign_client_id(campaign)
    donor_queryset = donor_queryset.filter(client_id=client_id)
    donor = donor_queryset.first()
    if donor is None:
        return None

    return {
        "donor": donor,
        "data_file_donor": None,
        "source": "house_file",
        "donor_name": get_donor_display_name(donor),
    }


def match_donor(urn: str, campaign: Any) -> dict[str, Any]:
    """Match an extracted URN to a donor record.

    For ``data_file`` campaigns the data file is searched first.  If the URN
    is not found there (or no data file has been uploaded yet), the house file
    is searched as a fallback so donors are still resolved when the data file
    arrives late.

    For ``house_file`` campaigns the house file is searched directly.

    Args:
        urn: The URN to match.
        campaign: Campaign model instance.

    Returns:
        dict with keys:
            - donor: Donor instance or None.
            - data_file_donor: DataFileDonor instance or None.
            - source: ``"data_file"``, ``"house_file"``, or ``"not_found"``.
            - donor_name: Resolved full name or empty string.
    """
    from django.core.exceptions import ObjectDoesNotExist

    not_found: dict[str, Any] = {
        "donor": None,
        "data_file_donor": None,
        "source": "not_found",
        "donor_name": "",
    }

    if not urn:
        return not_found

    donor_source = getattr(campaign, "donor_source", "house_file")

    if donor_source == "data_file":
        try:
            data_file = campaign.data_file
        except AttributeError, ObjectDoesNotExist:
            data_file = None

        if data_file is not None:
            data_file_match = match_data_file_donor(urn, data_file)
            if data_file_match is not None:
                return data_file_match
            logger.debug(
                "URN '%s' not found in data file for campaign %s; falling back to house file",
                urn,
                getattr(campaign, "id", "unknown"),
            )
        else:
            logger.warning(
                "Campaign %s has no data file; falling back to house file for URN '%s'",
                getattr(campaign, "id", "unknown"),
                urn,
            )

        # Fallback: search house file
        house_file_match = match_house_file_donor(urn, campaign)
        if house_file_match is not None:
            return house_file_match
        return not_found

    # house_file source: search house file directly
    house_file_match = match_house_file_donor(urn, campaign)
    if house_file_match is not None:
        return house_file_match

    return not_found
