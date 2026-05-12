"""Donor matching and review-metadata helpers for scan processing.

Donor-match thresholds (false-positive guard)
---------------------------------------------
Crediting a donation to the wrong donor is a high-impact data-quality bug:
the donor receives someone else's thank-you letter, the giving history is
corrupted, and Gift Aid claims may be filed against the wrong individual.
The previous matcher fell back to a case-insensitive ``first_name`` /
``last_name`` exact-match plus postcode check, which silently accepted any
two donors who happened to share a casefolded name (e.g. ``"John Smith"``
twice within the same postcode area, or trivial OCR variants).

This module now uses :mod:`rapidfuzz.distance.JaroWinkler` to gate the
name-based fallback. Identifier-based lookups (URN / email / phone) remain
authoritative — those are not fuzzy and stay the first choice.

Thresholds:

* ``AUTO_MATCH_SIMILARITY`` (``0.95``) — at or above this Jaro-Winkler
  similarity *and* with an exact (normalised) postcode match, the donor is
  auto-linked. Picked to allow trivial OCR / casing differences (e.g.
  ``"O'Brien"`` vs ``"OBrien"``) while still rejecting different surnames.
* ``CANDIDATE_SIMILARITY`` (``0.85``) — between this and
  ``AUTO_MATCH_SIMILARITY``, with postcode match, the donor is *not*
  auto-linked. Instead, the candidate IDs are surfaced to QA via the
  placeholder's review metadata and ultimately written to
  ``Donation.donor_match_candidates``. The reviewer picks the right donor
  or creates a new one.
* Below ``CANDIDATE_SIMILARITY`` *or* with a postcode mismatch, no match is
  suggested at all and the donation flows through with ``donor=None``.

Both thresholds are deliberately conservative — false positives from the
old behaviour are far more damaging than the marginal extra QA work caused
by sending borderline cases to manual review.
"""

import logging
from typing import Any

from django.db.models import Q
from rapidfuzz.distance import JaroWinkler

logger = logging.getLogger(__name__)


# Jaro-Winkler thresholds for the name-based donor fallback. See the
# module docstring for the rationale behind these numbers.
AUTO_MATCH_SIMILARITY = 0.95
CANDIDATE_SIMILARITY = 0.85


def split_donor_name(full_name: str) -> tuple[str, str]:
    """Split a full name string into first-name and last-name parts."""
    titles = {"mr", "mrs", "ms", "miss", "dr", "prof", "rev", "sir"}
    parts = full_name.split()
    if parts and parts[0].lower().rstrip(".") in titles:
        parts = parts[1:]
    if len(parts) > 1:
        return " ".join(parts[:-1]), parts[-1]
    return "", (parts[0] if parts else "")


def _bool_opt(extracted: dict[str, Any], key: str) -> bool | None:
    """Convert OCR yes/no style values into an optional boolean."""
    value = extracted.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"yes", "true", "1", "x"}


def _normalise_match_value(value: object) -> str:
    """Return a case-insensitive, trimmed value for exact donor matching."""
    return str(value or "").strip().casefold()


def _normalise_postcode(value: object) -> str:
    """Return a space-insensitive postcode value for exact donor matching."""
    return "".join(str(value or "").strip().upper().split())


def _postcode_filter_variants(normalised: str) -> list[str]:
    """Return likely stored postcode variants matching a normalised compact form.

    The DB stores postcodes as raw user/import input — typically either the
    compact form (``"SW1A1AA"``) or the canonical spaced form
    (``"SW1A 1AA"``). We pre-filter the candidate queryset against both so
    the Python-side normalisation check is only run over postcode-matching
    rows. Anything stored in a non-canonical form (e.g. extra whitespace) is
    rare enough that the modest extra QA cost from missing it is preferable
    to scanning every donor for the client.
    """
    if not normalised:
        return []
    variants = {normalised}
    if len(normalised) >= 5:
        variants.add(f"{normalised[:-3]} {normalised[-3:]}")
    return sorted(variants)


def _campaign_client(campaign: Any) -> Any:
    """Return the client attached to the campaign."""
    return campaign.client


def _record_fuzzy_candidates(
    placeholder: Any, candidates: list[tuple[Any, float]]
) -> None:
    """Stash fuzzy-match candidate donors on the placeholder for QA review.

    Two surfaces are populated for the transition window:

    * ``placeholder.donor_match_candidates`` (the dedicated JSON field on
      :class:`scans.models.ScanPlaceholder`) holds the canonical structured
      dicts — ``{"urn": ..., "score": ..., "name": ..., "system_donor_id": ...}``
      — so QA UIs can show similarity scores without a follow-up DB lookup.
      :func:`scans.scan_processing_donations.create_donation_from_placeholder`
      copies this payload directly onto
      :attr:`donations.models.Donation.donor_match_candidates`.
    * ``placeholder.ocr_data["donor_match_candidates"]`` is kept in sync as
      the legacy list of stringified ``SystemDonor`` PKs.

    .. deprecated::
        The dual write is transitional. Once the QA UI consumes the
        structured ``Donation.donor_match_candidates`` payload directly,
        delete the ``placeholder.ocr_data["donor_match_candidates"]`` write
        below (and the
        :func:`scans.scan_processing_donors.get_donor_match_candidates`
        fallback in
        :func:`scans.scan_processing_donations.create_donation_from_placeholder`).
        Tracked in the unit-24 follow-up clean-up.

    Args:
        placeholder: Scan placeholder being processed.
        candidates: List of ``(SystemDonor, score)`` tuples whose Jaro-Winkler
            similarity falls in ``[CANDIDATE_SIMILARITY, AUTO_MATCH_SIMILARITY)``.
    """
    if not candidates:
        return
    placeholder.donor_match_candidates = [
        {
            "urn": str(getattr(donor, "external_urn", "") or ""),
            "score": round(float(score), 4),
            "name": f"{donor.first_name} {donor.last_name}".strip(),
            "system_donor_id": str(donor.pk),
        }
        for donor, score in candidates
    ]
    # DEPRECATED: legacy bare-PK list kept on ocr_data so callers that have
    # not yet migrated to the structured payload (see docstring) keep
    # working. Remove together with ``get_donor_match_candidates``.
    ocr_data = dict(placeholder.ocr_data or {})
    ocr_data["donor_match_candidates"] = [str(c.pk) for c, _ in candidates]
    placeholder.ocr_data = ocr_data


def get_donor_match_candidates(placeholder: Any) -> list[str]:
    """Return the stringified donor IDs flagged as fuzzy candidates.

    .. deprecated::
        Reads the legacy ``placeholder.ocr_data["donor_match_candidates"]``
        bare PK list. Prefer the structured
        :attr:`scans.models.ScanPlaceholder.donor_match_candidates` JSON
        field directly. Kept only as a transition-period fallback for
        :func:`scans.scan_processing_donations.create_donation_from_placeholder`
        — delete once every reader migrates to the structured payload.
    """
    ocr_data = placeholder.ocr_data or {}
    raw = ocr_data.get("donor_match_candidates") or []
    return [str(item) for item in raw]


def _source_snapshot(
    *,
    donor: Any | None = None,
    data_file_donor: Any | None = None,
    placeholder: Any | None = None,
) -> dict[str, Any]:
    """Build a normalized donor snapshot from a source donor or OCR placeholder."""
    source = data_file_donor or donor
    if source is not None:
        return {
            "external_urn": str(getattr(source, "urn", "") or "").strip(),
            "title": str(getattr(source, "title", "") or "").strip(),
            "first_name": str(getattr(source, "first_name", "") or "").strip(),
            "last_name": str(getattr(source, "last_name", "") or "").strip()
            or "Unknown",
            "email": str(getattr(source, "email", "") or "").strip(),
            "phone": str(getattr(source, "phone", "") or "").strip(),
            "address_line1": str(getattr(source, "address_line1", "") or "").strip(),
            "address_line2": str(getattr(source, "address_line2", "") or "").strip(),
            "city": str(getattr(source, "city", "") or "").strip(),
            "county": str(getattr(source, "county", "") or "").strip(),
            "postcode": str(getattr(source, "postcode", "") or "").strip(),
            "country": str(getattr(source, "country", "") or "").strip()
            or "United Kingdom",
            "gift_aid_declaration": bool(
                getattr(source, "gift_aid_declaration", False) or False
            ),
            "gift_aid_date": getattr(source, "gift_aid_date", None),
            "consent_contact": bool(getattr(source, "consent_contact", False) or False),
            "opt_in_email": bool(getattr(source, "opt_in_email", False) or False),
            "opt_in_sms": bool(getattr(source, "opt_in_sms", False) or False),
            "opt_in_phone": bool(getattr(source, "opt_in_phone", False) or False),
            "opt_in_post": bool(getattr(source, "opt_in_post", False) or False),
            "contact_status": str(
                getattr(source, "contact_status", "normal") or "normal"
            ),
            "contact_status_reason": str(
                getattr(source, "contact_status_reason", "") or ""
            ).strip(),
            "contact_status_changed_at": getattr(
                source, "contact_status_changed_at", None
            ),
            "address_last_verified_at": getattr(
                source, "address_last_verified_at", None
            ),
        }

    extracted: dict[str, Any] = getattr(placeholder, "extracted_data", None) or {}
    donor_name = str(extracted.get("donor_name", "") or "").strip()
    first_name, last_name = split_donor_name(donor_name)
    return {
        "external_urn": "",
        "title": str(extracted.get("title", "") or "").strip(),
        "first_name": first_name,
        "last_name": last_name or "Unknown",
        "email": str(extracted.get("email", "") or "").strip(),
        "phone": str(extracted.get("phone", "") or "").strip(),
        "address_line1": str(extracted.get("address_line1", "") or "").strip(),
        "address_line2": str(extracted.get("address_line2", "") or "").strip(),
        "city": str(extracted.get("city", "") or "").strip(),
        "county": "",
        "postcode": str(extracted.get("postcode", "") or "").strip(),
        "country": "United Kingdom",
        "gift_aid_declaration": False,
        "gift_aid_date": None,
        "consent_contact": bool(_bool_opt(extracted, "contact_consent") or False),
        "opt_in_email": bool(_bool_opt(extracted, "email_consent") or False),
        "opt_in_sms": bool(_bool_opt(extracted, "sms_consent") or False),
        "opt_in_phone": bool(_bool_opt(extracted, "phone_consent") or False),
        "opt_in_post": bool(_bool_opt(extracted, "post_consent") or False),
        "contact_status": "normal",
        "contact_status_reason": "",
        "contact_status_changed_at": None,
        "address_last_verified_at": None,
    }


def _full_name_for_match(first_name: str, last_name: str) -> str:
    """Return a normalised full-name string used for similarity scoring."""
    return f"{first_name} {last_name}".strip().casefold()


def _name_similarity(a: str, b: str) -> float:
    """Return the Jaro-Winkler similarity (0..1) between two names."""
    if not a or not b:
        return 0.0
    return float(JaroWinkler.normalized_similarity(a, b))


def _name_postcode_match(
    campaign: Any, snapshot: dict[str, Any]
) -> tuple[Any | None, list[tuple[Any, float]]]:
    """Return the auto-match donor (if any) and any fuzzy candidates with scores.

    Applies the Jaro-Winkler thresholds documented in the module docstring.
    Postcodes must match exactly (after normalisation) for either an
    auto-match or a candidate suggestion — a postcode mismatch always
    rejects the row.

    Args:
        campaign: Campaign used to scope the search to a single client.
        snapshot: Donor snapshot built by :func:`_source_snapshot`.

    Returns:
        A two-tuple of ``(auto_match, fuzzy_candidates)``. ``auto_match`` is
        the :class:`~donors.models.SystemDonor` to auto-link, or ``None``
        when no donor crosses :data:`AUTO_MATCH_SIMILARITY`.
        ``fuzzy_candidates`` lists ``(donor, score)`` tuples for donors whose
        similarity falls in ``[CANDIDATE_SIMILARITY, AUTO_MATCH_SIMILARITY)``,
        sorted by descending score so the strongest hit appears first.
    """
    from donors.models import SystemDonor

    first_name = _normalise_match_value(snapshot.get("first_name"))
    last_name = _normalise_match_value(snapshot.get("last_name"))
    postcode = _normalise_postcode(snapshot.get("postcode"))
    if not (first_name and last_name and postcode):
        return None, []

    target_name = _full_name_for_match(first_name, last_name)

    # Pre-filter at the DB level on postcode. Stored postcodes are typically
    # the compact ("SW1A1AA") or canonical spaced ("SW1A 1AA") form, so we
    # query both case-insensitively rather than scanning every donor for the
    # client (clients with 100k+ donors otherwise force a full table scan
    # per placeholder). The Python-side ``_normalise_postcode`` guard below
    # remains the source of truth for the equality check, so any
    # non-canonical stored format that slips through the variant filter is
    # the only thing missed — an acceptable trade-off versus full scans.
    postcode_variants = _postcode_filter_variants(postcode)
    postcode_q = Q()
    for variant in postcode_variants:
        postcode_q |= Q(postcode__iexact=variant)
    candidate_qs = (
        SystemDonor.objects.filter(client=_campaign_client(campaign))
        .filter(postcode_q)
        .order_by("-updated_at")
    )
    auto_match: Any | None = None
    best_auto_score = 0.0
    fuzzy_candidates: list[tuple[Any, float]] = []

    for candidate in candidate_qs:
        if _normalise_postcode(candidate.postcode) != postcode:
            continue
        candidate_name = _full_name_for_match(
            _normalise_match_value(candidate.first_name),
            _normalise_match_value(candidate.last_name),
        )
        score = _name_similarity(target_name, candidate_name)
        if score >= AUTO_MATCH_SIMILARITY:
            if score > best_auto_score:
                auto_match = candidate
                best_auto_score = score
        elif score >= CANDIDATE_SIMILARITY:
            fuzzy_candidates.append((candidate, score))

    if auto_match is not None:
        # When we auto-match we deliberately drop the fuzzy list — the
        # auto-match wins and there is nothing for QA to disambiguate.
        return auto_match, []
    fuzzy_candidates.sort(key=lambda pair: pair[1], reverse=True)
    return None, fuzzy_candidates


def _find_existing_system_donor(
    campaign: Any, snapshot: dict[str, Any]
) -> tuple[Any | None, list[tuple[Any, float]]]:
    """Find the most likely existing internal donor for a snapshot.

    Identifier-based lookups (URN, email, phone) remain authoritative and
    return immediately when they hit. Only the name-based fallback applies
    the Jaro-Winkler thresholds described in the module docstring.

    Args:
        campaign: Campaign whose client scopes the lookup.
        snapshot: Donor snapshot from :func:`_source_snapshot`.

    Returns:
        ``(auto_match, fuzzy_candidates)``. ``auto_match`` is the donor to
        auto-link or ``None``; ``fuzzy_candidates`` is a list of
        ``(donor, score)`` tuples for QA review (always empty when
        ``auto_match`` is set or when an identifier match wins).
    """
    from donors.models import SystemDonor

    client = _campaign_client(campaign)
    external_urn = str(snapshot.get("external_urn") or "").strip()
    if external_urn:
        existing = SystemDonor.objects.filter(
            client=client,
            external_urn__iexact=external_urn,
        ).first()
        if existing is not None:
            return existing, []

    email = _normalise_match_value(snapshot.get("email"))
    if email:
        existing = SystemDonor.objects.filter(
            client=client, email__iexact=email
        ).first()
        if existing is not None:
            return existing, []

    phone = _normalise_match_value(snapshot.get("phone"))
    if phone:
        existing = SystemDonor.objects.filter(client=client, phone=phone).first()
        if existing is not None:
            return existing, []

    return _name_postcode_match(campaign, snapshot)


def _upsert_system_donor(
    campaign: Any,
    *,
    donor: Any | None = None,
    data_file_donor: Any | None = None,
    placeholder: Any | None = None,
    created_by: Any | None = None,
    pending_review: bool = False,
) -> Any:
    """Create or update the internal donor profile from source or OCR data."""
    from donors.models import SystemDonor

    snapshot = _source_snapshot(
        donor=donor,
        data_file_donor=data_file_donor,
        placeholder=placeholder,
    )
    existing, fuzzy_candidates = _find_existing_system_donor(campaign, snapshot)
    if placeholder is not None and existing is None and fuzzy_candidates:
        # Surface borderline matches to QA so the reviewer can disambiguate
        # rather than silently auto-linking a low-confidence match.
        _record_fuzzy_candidates(placeholder, fuzzy_candidates)
    defaults = {
        "client": _campaign_client(campaign),
        "title": snapshot["title"],
        "first_name": snapshot["first_name"] or "Unknown",
        "last_name": snapshot["last_name"] or "Unknown",
        "email": snapshot["email"],
        "phone": snapshot["phone"],
        "address_line1": snapshot["address_line1"],
        "address_line2": snapshot["address_line2"],
        "city": snapshot["city"],
        "county": snapshot["county"],
        "postcode": snapshot["postcode"],
        "country": snapshot["country"],
        "gift_aid_declaration": snapshot["gift_aid_declaration"],
        "gift_aid_date": snapshot["gift_aid_date"],
        "contact_status": snapshot["contact_status"],
        "contact_status_reason": snapshot["contact_status_reason"],
        "contact_status_changed_at": snapshot["contact_status_changed_at"],
        "address_last_verified_at": snapshot["address_last_verified_at"],
        "consent_contact": snapshot["consent_contact"],
        "opt_in_email": snapshot["opt_in_email"],
        "opt_in_sms": snapshot["opt_in_sms"],
        "opt_in_phone": snapshot["opt_in_phone"],
        "opt_in_post": snapshot["opt_in_post"],
        "source_snapshot": snapshot,
    }
    external_urn = str(snapshot.get("external_urn") or "").strip()

    if existing is None:
        return SystemDonor.objects.create(
            external_urn=external_urn,
            created_by=created_by,
            pending_review=pending_review,
            **defaults,
        )

    for field_name, value in defaults.items():
        if value not in ("", None, {}):
            setattr(existing, field_name, value)
    if external_urn:
        existing.external_urn = external_urn
    existing.save()
    return existing


def upsert_system_donor_from_source(
    campaign: Any,
    *,
    donor: Any | None = None,
    data_file_donor: Any | None = None,
    placeholder: Any | None = None,
    created_by: Any | None = None,
) -> Any:
    """Public wrapper used by non-scan code paths to upsert system donors."""
    return _upsert_system_donor(
        campaign,
        donor=donor,
        data_file_donor=data_file_donor,
        placeholder=placeholder,
        created_by=created_by,
    )


def _donor_create_kwargs(placeholder: Any, campaign: Any) -> dict[str, Any]:
    """Build the kwargs used to create a donor from OCR data."""
    create_kwargs = _source_snapshot(placeholder=placeholder)
    create_kwargs["created_by"] = placeholder.batch.created_by
    create_kwargs["client"] = _campaign_client(campaign)
    return create_kwargs


def _find_reusable_placeholder_donor(placeholder: Any, campaign: Any) -> Any | None:
    """Return an existing unresolved donor when the OCR identity is exact enough.

    Borderline (fuzzy) candidates do **not** count as reusable — they are
    instead recorded on the placeholder so QA can disambiguate. Only an
    auto-match clears the threshold.
    """
    snapshot = _donor_create_kwargs(placeholder, campaign)
    auto_match, fuzzy_candidates = _find_existing_system_donor(campaign, snapshot)
    if auto_match is None and fuzzy_candidates:
        _record_fuzzy_candidates(placeholder, fuzzy_candidates)
    return auto_match


def create_new_placeholder_donor(placeholder: Any, campaign: Any) -> Any:
    """Create or reuse an internal donor profile for an unmatched scan."""
    extracted: dict[str, Any] = placeholder.extracted_data or {}
    donor_name = str(extracted.get("donor_name", "") or "").strip()
    postcode = str(extracted.get("postcode", "") or "").strip()
    existing_donor = _find_reusable_placeholder_donor(placeholder, campaign)
    if existing_donor is not None:
        logger.info(
            "Reused placeholder donor %s (name=%r, postcode=%r) for scan %s",
            existing_donor.pk,
            donor_name,
            postcode,
            placeholder.id,
        )
        return existing_donor

    donor = _upsert_system_donor(
        campaign,
        placeholder=placeholder,
        created_by=placeholder.batch.created_by,
        pending_review=True,
    )
    logger.info(
        "Created internal donor %s (name=%r, postcode=%r) for scan %s",
        donor.pk,
        donor_name,
        postcode,
        placeholder.id,
    )
    return donor


def _sync_existing_donations(placeholder: Any, donor: Any) -> None:
    """Update existing donations linked to the placeholder to use the system donor."""
    from donations.models import Donation

    if donor.__class__.__name__ != "SystemDonor":
        return

    updated = (
        Donation.objects.filter(scan_placeholder=placeholder)
        .exclude(system_donor=donor)
        .update(system_donor=donor, donor=None, data_file_donor=None)
    )
    if updated:
        logger.debug(
            "Synced system donor %s to %d donation(s) for placeholder %s",
            donor.pk,
            updated,
            placeholder.pk,
        )


def _donor_updates_from_extracted(
    placeholder: Any, donor: Any
) -> tuple[str, dict[str, Any]]:
    """Return the scan purpose and donor-field updates derived from OCR data."""
    extracted: dict[str, Any] = getattr(placeholder, "extracted_data", None) or {}
    scan_purpose = (
        placeholder.batch.campaign.scan_purpose
        if placeholder.batch and placeholder.batch.campaign
        else "donation"
    )
    overwrite = scan_purpose == "donor_update"
    field_map = [
        ("title", "title"),
        ("phone", "phone"),
        ("email", "email"),
        ("address_line1", "address_line1"),
        ("address_line2", "address_line2"),
        ("city", "city"),
        ("postcode", "postcode"),
    ]

    donor_updates: dict[str, Any] = {}
    for extracted_key, donor_field in field_map:
        new_value = str(extracted.get(extracted_key, "") or "").strip()
        if not new_value:
            continue
        existing = str(getattr(donor, donor_field, "") or "").strip()
        if overwrite or not existing:
            donor_updates[donor_field] = new_value
    return scan_purpose, donor_updates


def sync_donation_donor(placeholder: Any, donor: Any) -> None:
    """Update linked donations and optionally patch donor fields from OCR.

    OCR values are written back to the donor record **only** when the campaign
    ``scan_purpose`` is ``donor_update``.  For all other matched donors the DB
    record is left exactly as-is so the QA screen always shows the authoritative
    house-file / data-file data rather than OCR-extracted values.
    """
    _sync_existing_donations(placeholder, donor)
    scan_purpose, donor_updates = _donor_updates_from_extracted(placeholder, donor)

    if scan_purpose != "donor_update":
        # Do not overwrite matched donor records with OCR data; QA must always
        # show the house-file / data-file values.
        return

    if not donor_updates:
        return

    for field_name, value in donor_updates.items():
        setattr(donor, field_name, value)
    donor.save(update_fields=[*donor_updates.keys(), "updated_at"])
    logger.info(
        "Overwrote donor %s fields from OCR (scan_purpose=donor_update): %s",
        donor.pk,
        list(donor_updates.keys()),
    )


def _identifier_source(placeholder: Any, campaign_temperature: str) -> str:
    """Return the identifier source used for review metadata."""
    if placeholder.qr_decoded:
        return "qr"
    if campaign_temperature == "warm":
        return "none"
    if placeholder.urn:
        return "ocr"
    return "none"


def set_review_metadata(
    placeholder: Any,
    campaign: Any,
    donor_match_status: str,
    exception_reason: str,
) -> None:
    """Store review metadata for downstream QA rendering."""
    campaign_temperature = getattr(campaign, "campaign_temperature", "cold")
    ocr_data = dict(placeholder.ocr_data or {})
    ocr_data["campaign_temperature"] = campaign_temperature
    ocr_data["identifier_source"] = _identifier_source(
        placeholder,
        campaign_temperature,
    )
    ocr_data["donor_match_status"] = donor_match_status
    ocr_data["exception_reason"] = exception_reason
    placeholder.ocr_data = ocr_data


def get_review_metadata(placeholder: Any, campaign: Any) -> dict[str, str]:
    """Return normalized review metadata for a processed placeholder."""
    ocr_data = dict(placeholder.ocr_data or {})
    return {
        "campaign_temperature": str(
            ocr_data.get("campaign_temperature")
            or getattr(campaign, "campaign_temperature", "cold")
        ),
        "identifier_source": str(ocr_data.get("identifier_source") or "none"),
        "donor_match_status": str(
            ocr_data.get("donor_match_status") or "manual_review"
        ),
        "exception_reason": str(ocr_data.get("exception_reason") or ""),
    }


def describe_exception_reason(exception_reason: str) -> str:
    """Convert an exception code into a QA-facing sentence."""
    messages = {
        "qr_unreadable_for_warm_campaign": (
            "Readable QR code was not found for this warm campaign. Review manually "
            "and rescan under the cold campaign if required."
        ),
        "qr_malformed_for_warm_campaign": (
            "QR code was detected on this warm record but its payload is corrupt. "
            "Rescan the form so the QR can be re-read."
        ),
        "qr_malformed_payload": (
            "QR code was detected but its payload is corrupt. Rescan the form so "
            "the QR can be re-read."
        ),
        "warm_source_miss_rescan_under_cold_campaign": (
            "QR donor was not found in the configured source. Rescan this record "
            "under the cold campaign."
        ),
        "cold_unmatched_donor_created": (
            "A new donor record was created during cold-campaign capture. Verify "
            "donor identity and address before approval."
        ),
    }
    return messages.get(exception_reason, "")


def _manual_review_response() -> dict[str, Any]:
    """Return the standard donor-match response for manual review cases."""
    return {
        "donor": None,
        "data_file_donor": None,
        "source": "manual_review",
        "donor_name": "",
    }


def _apply_warm_qr_failure(
    placeholder: Any, campaign: Any, exception_reason: str
) -> dict[str, Any]:
    """Mark a warm-campaign placeholder for manual review and rescan."""
    from scans.models import ScanPlaceholder

    placeholder.ocr_status = ScanPlaceholder.OCR_STATUS_NEEDS_RESCAN
    placeholder.urn = ""
    placeholder.matched_donor = None
    placeholder.matched_data_file_donor = None
    placeholder.donor_name = ""
    if exception_reason == "qr_unreadable_for_warm_campaign":
        placeholder.processing_error = (
            "Warm campaign requires a readable QR code. Review manually and rescan "
            "under the cold campaign if required."
        )
    elif exception_reason == "qr_malformed_for_warm_campaign":
        placeholder.processing_error = _malformed_qr_processing_error(placeholder)
    else:
        source_label = (
            "data file" if campaign.donor_source == "data_file" else "house file"
        )
        placeholder.processing_error = (
            f"QR URN '{placeholder.urn or '(blank)'}' was not found in the "
            f"configured {source_label}. Rescan this record under the cold campaign."
        )
    set_review_metadata(
        placeholder,
        campaign,
        donor_match_status="manual_review",
        exception_reason=exception_reason,
    )
    return _manual_review_response()


def _qr_kind_from_placeholder(placeholder: Any) -> str:
    """Return the QR result kind stored on the placeholder by ``decode_qr_from_scan``."""
    ocr_data = placeholder.ocr_data or {}
    return str(ocr_data.get("qr_kind") or "")


def _malformed_qr_processing_error(placeholder: Any) -> str:
    """Build the QA-facing rescan message for a malformed QR payload."""
    ocr_data = placeholder.ocr_data or {}
    qr_error = str(ocr_data.get("qr_error") or "")
    detail = f" ({qr_error})" if qr_error else ""
    return (
        f"QR code was detected but its payload is corrupt{detail}. Rescan this "
        "form so the QR can be re-read."
    )


def _apply_malformed_qr_rescan(placeholder: Any, campaign: Any) -> dict[str, Any]:
    """Flag a placeholder whose QR sticker was detected but unreadable for rescan."""
    from scans.models import ScanPlaceholder

    placeholder.ocr_status = ScanPlaceholder.OCR_STATUS_COMPLETED
    placeholder.matched_donor = None
    placeholder.matched_data_file_donor = None
    placeholder.donor_name = ""
    # NOTE: Unlike the warm path, we deliberately preserve placeholder.urn here.
    # On the cold path the URN may have been populated from a filename fallback
    # (e.g. batch_naming inferred a URN from the scan filename), and clearing it
    # would lose that operator-friendly hint. The QR payload itself is corrupt
    # and won't be trusted for matching, but the filename URN is still useful
    # context when QA chases the rescan.
    placeholder.processing_error = _malformed_qr_processing_error(placeholder)
    set_review_metadata(
        placeholder,
        campaign,
        donor_match_status="manual_review",
        exception_reason="qr_malformed_payload",
    )
    return _manual_review_response()


def _cold_unmatched_processing_error(placeholder: Any, campaign: Any) -> None:
    """Populate the default cold-campaign unmatched donor warning."""
    if placeholder.processing_error:
        return

    # For data_file campaigns the system searches both data file and house file
    # before giving up, so the message reflects both sources.
    source_label = (
        "data file or house file"
        if campaign.donor_source == "data_file"
        else "house file"
    )
    donor_label = (
        "New donor record created and marked unverified."
        if campaign.donor_source == "data_file"
        else "New donor record created and marked pending export."
    )
    placeholder.processing_error = (
        f"URN '{placeholder.urn or '(blank)'}' was not found in {source_label}. "
        f"{donor_label}"
    )


def apply_donor_match(placeholder: Any, campaign: Any) -> dict[str, Any]:
    """Match a placeholder's URN to a donor and update status metadata."""
    from scans.models import ScanPlaceholder
    from scans.ocr import OCRExtractor
    from scans.qr import QRResultKind

    qr_kind = _qr_kind_from_placeholder(placeholder)
    campaign_temperature = getattr(campaign, "campaign_temperature", "cold")

    if qr_kind == QRResultKind.MALFORMED:
        if campaign_temperature == "warm":
            return _apply_warm_qr_failure(
                placeholder,
                campaign,
                "qr_malformed_for_warm_campaign",
            )
        return _apply_malformed_qr_rescan(placeholder, campaign)

    if campaign_temperature == "warm" and not placeholder.qr_decoded:
        return _apply_warm_qr_failure(
            placeholder,
            campaign,
            "qr_unreadable_for_warm_campaign",
        )

    match_result = OCRExtractor.match_donor(placeholder.urn, campaign)
    placeholder.matched_donor = match_result["donor"]
    placeholder.matched_data_file_donor = match_result["data_file_donor"]
    placeholder.donor_name = match_result["donor_name"]

    if match_result["source"] != "not_found":
        placeholder.matched_system_donor = _upsert_system_donor(
            campaign,
            donor=match_result["donor"],
            data_file_donor=match_result["data_file_donor"],
            created_by=getattr(placeholder.batch, "created_by", None),
        )
        placeholder.ocr_status = ScanPlaceholder.OCR_STATUS_MATCHED
        set_review_metadata(
            placeholder,
            campaign,
            donor_match_status="matched",
            exception_reason="",
        )
        return match_result

    placeholder.ocr_status = ScanPlaceholder.OCR_STATUS_UNMATCHED
    if campaign_temperature == "warm":
        return _apply_warm_qr_failure(
            placeholder,
            campaign,
            "warm_source_miss_rescan_under_cold_campaign",
        )

    _cold_unmatched_processing_error(placeholder, campaign)
    new_donor = create_new_placeholder_donor(placeholder, campaign)
    placeholder.matched_donor = None
    placeholder.matched_data_file_donor = None
    placeholder.matched_system_donor = new_donor
    placeholder.donor_name = new_donor.full_name or "New Donor"
    set_review_metadata(
        placeholder,
        campaign,
        donor_match_status="new_donor_created",
        exception_reason="cold_unmatched_donor_created",
    )
    return {
        **match_result,
        "donor": None,
        "system_donor": new_donor,
        "source": "house_file",
        "donor_name": placeholder.donor_name,
    }
