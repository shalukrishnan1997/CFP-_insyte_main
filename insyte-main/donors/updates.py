"""Shared donor-mutation helpers used by QA review and phone intake.

Mutations are committed immediately. Callers wrap a compound (donor mutate +
related row create) in a single ``transaction.atomic()`` block so a failure
in either step rolls back both.

QA rejection of a phone-intake non-financial donation does not revert the
donor mutation that happened at intake — the QA verdict is a paper-trail flag
only. Reverts go through Django admin.

Audit logging is automatic: ``Donor``, ``DataFileDonor`` and ``SystemDonor``
are listed in :data:`audit.signals.AUDITED_MODELS`, so pre/post-save signals
record field-level diffs on every ``.save()``. Callers must not write manual
``AuditLog.objects.create`` calls from this module.
"""

import logging
from datetime import datetime
from typing import Final

from django.db import IntegrityError, connection, models, transaction
from django.db.models import QuerySet
from django.utils import timezone

from core.date_utils import parse_date
from donations.models import Donation
from donors.models import DataFileDonor, Donor, SystemDonor

logger = logging.getLogger(__name__)


type _DonorModel = type[Donor] | type[DataFileDonor] | type[SystemDonor]


class DonorVanished(Exception):
    """Raised when a row-locked donor cannot be loaded.

    Indicates the donor row was deleted between page-load and submit.
    Views should translate this to HTTP 410 Gone.
    """


# ---------------------------------------------------------------------------
# Row-lock helpers (lifted from custom_admin.views.qa_review)
# ---------------------------------------------------------------------------


def _supports_row_locks() -> bool:
    """Whether the active DB backend honours ``SELECT ... FOR UPDATE``.

    SQLite (used in dev/test) silently no-ops ``select_for_update`` and does
    not support the ``of=`` argument. Production (Postgres) does — gate any
    ``of=`` calls on this so the dev/test path stays portable.
    """
    return connection.vendor != "sqlite"


def _lock_self[T: models.Model](manager: models.Manager[T]) -> QuerySet[T]:
    """Return a queryset that locks only the manager's own table rows.

    Encapsulates the Postgres/SQLite split: on Postgres we pass
    ``of=("self",)`` so joined tables are not also locked; on SQLite the
    argument is unsupported and the lock itself is a no-op.
    """
    if _supports_row_locks():
        return manager.select_for_update(of=("self",))
    return manager.select_for_update()


# ---------------------------------------------------------------------------
# POST helpers (kept module-local; callers should not depend on these)
# ---------------------------------------------------------------------------


def _bool_from_post(data: dict[str, object], key: str) -> bool:
    return data.get(key) in {"on", "true", "1", True}


# ---------------------------------------------------------------------------
# Field maps
# ---------------------------------------------------------------------------


_DONOR_TEXT_FIELDS: Final[dict[str, str]] = {
    "urn": "donor_urn",
    "title": "donor_title",
    "first_name": "donor_first_name",
    "last_name": "donor_last_name",
    "email": "donor_email",
    "phone": "donor_phone",
    "address_line1": "donor_address_line1",
    "address_line2": "donor_address_line2",
    "city": "donor_city",
    "county": "donor_county",
    "postcode": "donor_postcode",
    "country": "donor_country",
}

_DONOR_BOOL_FIELDS: Final[dict[str, str]] = {
    "no_thank_you": "donor_no_thank_you",
    "opt_in_email": "donor_opt_in_email",
    "opt_in_sms": "donor_opt_in_sms",
    "opt_in_phone": "donor_opt_in_phone",
    "opt_in_post": "donor_opt_in_post",
    "consent_contact": "donor_consent_contact",
    "gift_aid_declaration": "donor_gift_aid_declaration",
}

_DONOR_DATE_FIELDS: Final[dict[str, str]] = {
    "date_of_birth": "donor_date_of_birth",
    "gift_aid_date": "donor_gift_aid_date",
}

_DONOR_INT_FIELDS: Final[dict[str, str]] = {
    "age": "donor_age",
}


# Default editable surface for QA review (preserves pre-existing behaviour).
# Excludes ``consent_contact``, ``date_of_birth``, ``age``,
# ``gift_aid_declaration``, ``gift_aid_date`` — the QA donation-review form
# does not currently render those inputs, so they must not be cleared by a
# QA save that simply omits them.
_QA_DEFAULT_EDITABLE: Final[frozenset[str]] = frozenset(
    {
        *_DONOR_TEXT_FIELDS.keys(),
        "no_thank_you",
        "opt_in_email",
        "opt_in_sms",
        "opt_in_phone",
        "opt_in_post",
    }
)


# Full editable surface exposed to phone-intake operators.
PHONE_INTAKE_EDITABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        *_DONOR_TEXT_FIELDS.keys(),
        *_DONOR_BOOL_FIELDS.keys(),
        *_DONOR_DATE_FIELDS.keys(),
        *_DONOR_INT_FIELDS.keys(),
        "contact_status",
    }
)


# ---------------------------------------------------------------------------
# Field-level micro-helpers
# ---------------------------------------------------------------------------


type _DonorLike = Donor | DataFileDonor | SystemDonor


def _update_text_field(
    obj: _DonorLike,
    data: dict[str, object],
    field: str,
    updated: list[str],
    *,
    post_key: str | None = None,
) -> None:
    raw = data.get(post_key or field)
    value = str(raw).strip() if raw is not None else ""
    if value != getattr(obj, field, ""):
        setattr(obj, field, value)
        updated.append(field)


def _update_bool_field(
    obj: _DonorLike,
    data: dict[str, object],
    field: str,
    updated: list[str],
    *,
    post_key: str | None = None,
) -> None:
    value = _bool_from_post(data, post_key or field)
    if hasattr(obj, field) and value != getattr(obj, field):
        setattr(obj, field, value)
        updated.append(field)


def _update_date_field(
    obj: _DonorLike,
    data: dict[str, object],
    field: str,
    updated: list[str],
    *,
    post_key: str | None = None,
) -> None:
    key = post_key or field
    if key not in data:
        return
    raw = data.get(key)
    value = parse_date(raw if isinstance(raw, str) else None)
    if value != getattr(obj, field, None):
        setattr(obj, field, value)
        updated.append(field)


def _update_int_field(
    obj: _DonorLike,
    data: dict[str, object],
    field: str,
    updated: list[str],
    *,
    post_key: str | None = None,
) -> None:
    raw = data.get(post_key or field)
    new_value: int | None
    if raw is None or raw == "":
        new_value = None
    else:
        text = str(raw).strip()
        if not text:
            new_value = None
        else:
            try:
                parsed = int(text)
            except ValueError:
                return
            if parsed < 0:
                return
            new_value = parsed
    if hasattr(obj, field) and new_value != getattr(obj, field):
        setattr(obj, field, new_value)
        updated.append(field)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_donor_field_updates(
    donor: Donor | DataFileDonor | SystemDonor,
    data: dict[str, object],
    *,
    editable_fields: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Apply POST data to a single donor, persisting via ``update_fields``.

    Args:
        donor: The donor row to mutate (any of the three donor models).
        data: POST-shaped dict; keys follow the ``donor_<field>`` pattern.
        editable_fields: When supplied, only these field names are read from
            ``data``. Defaults to the QA-review surface (preserves pre-existing
            behaviour). Phone-intake passes :data:`PHONE_INTAKE_EDITABLE_FIELDS`.

    Returns:
        List of mutated field names (deduplicated, preserves first-seen order).
    """
    allowed = (
        frozenset(editable_fields)
        if editable_fields is not None
        else _QA_DEFAULT_EDITABLE
    )
    updated: list[str] = []

    # URN handling (special-cased for nullability and SystemDonor.external_urn).
    # Only touched when ``donor_urn`` is actually present in the payload —
    # absent key means "no change" so partial-update callers don't blow away
    # an existing URN.
    if "urn" in allowed and "donor_urn" in data:
        raw = data.get("donor_urn")
        raw_urn = str(raw).strip() if raw is not None else ""
        new_urn: str | None = raw_urn if raw_urn else None
        if hasattr(donor, "external_urn"):
            # SystemDonor stores the absence of a URN as ``""`` (the
            # CharField default), not ``None``. Normalise both sides to the
            # same equivalence class so a no-op submission of an empty URN
            # against an empty stored value doesn't trigger a phantom save.
            current_external_urn = donor.external_urn or None
            if new_urn != current_external_urn:
                donor.external_urn = new_urn or ""
                updated.append("external_urn")
        elif new_urn != donor.urn:
            # DataFileDonor.urn is NOT NULL — refuse to clear an existing URN
            # to None. A blank submission is treated as "no change" rather
            # than an IntegrityError on save.
            if new_urn is None and isinstance(donor, DataFileDonor):
                pass
            else:
                donor.urn = new_urn
                updated.append("urn")

    for attr, post_key in _DONOR_TEXT_FIELDS.items():
        if attr == "urn" or attr not in allowed:
            continue
        _update_text_field(donor, data, attr, updated, post_key=post_key)

    for attr, post_key in _DONOR_BOOL_FIELDS.items():
        if attr not in allowed:
            continue
        _update_bool_field(donor, data, attr, updated, post_key=post_key)

    for attr, post_key in _DONOR_DATE_FIELDS.items():
        if attr not in allowed:
            continue
        _update_date_field(donor, data, attr, updated, post_key=post_key)

    for attr, post_key in _DONOR_INT_FIELDS.items():
        if attr not in allowed:
            continue
        _update_int_field(donor, data, attr, updated, post_key=post_key)

    if "contact_status" in allowed:
        raw_status_value = data.get("donor_contact_status")
        raw_status = (
            str(raw_status_value).strip() if raw_status_value is not None else ""
        )
        if raw_status:
            valid_statuses = {value for value, _ in Donor.CONTACT_STATUS_CHOICES}
            if raw_status in valid_statuses:
                raw_reason = data.get("donor_contact_status_reason")
                reason = str(raw_reason).strip() if raw_reason is not None else ""
                # Equality guard: only stamp ``contact_status_changed_at``
                # when the status or reason actually changed. Otherwise
                # every full-payload submission (now the norm for all
                # phone-intake payment methods) would falsely advance the
                # timestamp and pollute the audit log.
                current_status = donor.contact_status or ""
                current_reason = donor.contact_status_reason or ""
                if raw_status != current_status or reason != current_reason:
                    donor.contact_status = raw_status
                    donor.contact_status_reason = reason
                    donor.contact_status_changed_at = timezone.now()
                    updated.extend(
                        [
                            "contact_status",
                            "contact_status_reason",
                            "contact_status_changed_at",
                        ]
                    )

    if not updated:
        return []

    # SystemDonor.save() recomputes ``normalized_phone`` from ``phone``, but
    # ``update_fields=["phone"]`` excludes the recomputed value from the DB
    # write. Include ``normalized_phone`` whenever ``phone`` changed for a
    # SystemDonor so the indexed lookup column stays consistent.
    if "phone" in updated and isinstance(donor, SystemDonor):
        updated.append("normalized_phone")

    donor.save(update_fields=list(dict.fromkeys(updated)))
    return list(dict.fromkeys(updated))


def update_donor_from_post(
    donation: Donation,
    data: dict[str, object],
    *,
    editable_fields: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Apply POST data to the donor attached to *donation*.

    QA-review entry point. Resolves the donor off the donation, applies the
    cross-donor URN sibling-sync rules, and delegates per-field work to
    :func:`apply_donor_field_updates`.
    """
    donor = donation.system_donor or donation.donor or donation.data_file_donor
    source_donor = donation.donor or donation.data_file_donor
    if not donor:
        return []

    allowed = (
        frozenset(editable_fields)
        if editable_fields is not None
        else _QA_DEFAULT_EDITABLE
    )

    # URN sibling sync — only relevant for the QA path where a Donation links
    # both a system donor and an imported source donor. Only fires when the
    # caller actually submitted a ``donor_urn`` value.
    source_updated: list[str] = []
    original_source_urn = getattr(source_donor, "urn", None) if source_donor else None

    if (
        "urn" in allowed
        and "donor_urn" in data
        and source_donor is not None
        and source_donor is not donor
    ):
        raw = data.get("donor_urn")
        raw_urn = str(raw).strip() if raw is not None else ""
        new_urn: str | None = raw_urn if raw_urn else None
        source_has_existing_urn = bool((source_donor.urn or "").strip())
        # DataFileDonor.urn is NOT NULL — refuse to clear it to None.
        if new_urn is None and isinstance(source_donor, DataFileDonor):
            pass
        elif new_urn != source_donor.urn and (
            not source_has_existing_urn
            or source_donor.urn == new_urn
            or not hasattr(donor, "external_urn")
        ):
            source_donor.urn = new_urn
            source_updated.append("urn")

    primary_updated = apply_donor_field_updates(donor, data, editable_fields=allowed)

    # Mirror text/bool fields onto the source donor when distinct.
    if source_donor is not None and source_donor is not donor:
        mirror: list[str] = []
        for attr, post_key in _DONOR_TEXT_FIELDS.items():
            if attr == "urn" or attr not in allowed:
                continue
            if hasattr(source_donor, attr):
                _update_text_field(source_donor, data, attr, mirror, post_key=post_key)
        for attr, post_key in _DONOR_BOOL_FIELDS.items():
            if attr not in allowed:
                continue
            if hasattr(source_donor, attr):
                _update_bool_field(source_donor, data, attr, mirror, post_key=post_key)
        source_updated.extend(mirror)

    if source_updated:
        try:
            source_donor.save(  # type: ignore[union-attr]
                update_fields=list(dict.fromkeys(source_updated))
            )
        except IntegrityError:
            if "urn" not in source_updated:
                raise
            urn_raw = data.get("donor_urn")
            logger.warning(
                "Skipped syncing source donor URN during QA review due to a "
                "uniqueness conflict",
                extra={
                    "donation_id": str(donation.id),
                    "source_donor_model": source_donor.__class__.__name__,  # type: ignore[union-attr]
                    "source_donor_id": str(getattr(source_donor, "id", "")),
                    "submitted_urn": str(urn_raw).strip()
                    if urn_raw is not None
                    else "",
                },
            )
            source_donor.urn = original_source_urn  # type: ignore[union-attr]
            source_updated = [field for field in source_updated if field != "urn"]
            if source_updated:
                source_donor.save(  # type: ignore[union-attr]
                    update_fields=list(dict.fromkeys(source_updated))
                )

    return list(dict.fromkeys([*primary_updated, *source_updated]))


def apply_donor_contact_status(
    donor_links: list[tuple[_DonorModel, int]],
    *,
    contact_status: str,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Row-locked contact-status update across one or more linked donor rows.

    Used by the QA-side handler (which links up to three donor types per
    donation). Each row is re-fetched under ``SELECT FOR UPDATE`` to prevent
    concurrent reviewers from losing each other's writes.

    Raises:
        ValueError: ``contact_status`` is not in
            :attr:`Donor.CONTACT_STATUS_CHOICES`.
    """
    valid_statuses = {value for value, _ in Donor.CONTACT_STATUS_CHOICES}
    if contact_status not in valid_statuses:
        raise ValueError(f"Invalid contact_status: {contact_status!r}")

    if now is None:
        now = timezone.now()

    update_fields = [
        "contact_status",
        "contact_status_reason",
        "contact_status_changed_at",
    ]
    with transaction.atomic():
        for model, pk in donor_links:
            target = _lock_self(model.objects).get(pk=pk)
            target.contact_status = contact_status
            target.contact_status_reason = reason
            target.contact_status_changed_at = now
            target.save(update_fields=update_fields)
