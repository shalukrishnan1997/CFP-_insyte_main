"""QA review helpers, decorators, and data-update utilities.

Extracted from ``qa_review.py`` to reduce file size and improve testability.
"""

import logging
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, cast

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse

from core.date_utils import parse_date
from core.models import User
from donations.models import Donation
from responsehandling.permissions import is_authenticated_and_is_staff

logger = logging.getLogger(__name__)

ADMIN_GROUP_NAME = "admin"


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------


def has_qa_access(user: User) -> bool:
    """Return ``True`` if *user* may access QA features.

    Grants access to superusers, staff, admin-group members, QA-group members,
    or holders of any ``view_donationbatch`` permission regardless of app
    label. The bare-codename suffix-match keeps QA-group users working when
    ``DonationBatch`` migrates between Django apps (e.g. the original
    ``core`` → ``donations`` split) without requiring permission rows to be
    re-granted.

    Args:
        user: The user to check.

    Returns:
        Whether QA access is allowed.
    """
    if user.is_superuser or user.is_staff:
        return True
    if user.groups.filter(name__iexact=ADMIN_GROUP_NAME).exists():
        return True
    if user.groups.filter(name__iexact="QA").exists():
        return True
    return any(
        perm.endswith(".view_donationbatch") for perm in user.get_all_permissions()
    )


def qa_access_required(
    view_func: Callable[..., HttpResponse],
) -> Callable[..., HttpResponse]:
    """Decorator enforcing QA access rules with a 403 for unauthorised users."""

    @is_authenticated_and_is_staff
    def _wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not has_qa_access(cast(User, request.user)):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    _wrapped.__name__ = view_func.__name__
    _wrapped.__doc__ = view_func.__doc__
    return _wrapped


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


# parse_date is imported from core.date_utils (supports ISO + UK + named formats)


def parse_decimal(value: str | None, fallback: Decimal) -> Decimal:
    """Parse a decimal string with a fallback default.

    Args:
        value: Raw string value.
        fallback: Value to return if parsing fails.

    Returns:
        Parsed ``Decimal`` or *fallback*.
    """
    if value is None or value == "":
        return fallback
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):  # fmt: skip
        return fallback


def bool_from_post(data: dict[str, object], key: str) -> bool:
    """Extract a boolean from POST data (handles ``on``, ``true``, ``1``).

    Args:
        data: POST dictionary.
        key: Key to look up.

    Returns:
        ``True`` if the value indicates a truthy checkbox-style value.
    """
    return data.get(key) in {"on", "true", "1", True}


# ---------------------------------------------------------------------------
# Donor helpers
# ---------------------------------------------------------------------------


def get_donation_display_urn(donation: Donation) -> str:
    """Return the best available URN for donor-facing QA displays."""
    source_donor = (
        donation.data_file_donor or donation.donor
        if donation.donor_source == "data_file"
        else donation.donor or donation.data_file_donor
    )
    if donation.system_donor_id and donation.system_donor is not None:
        return (
            donation.system_donor.external_urn or getattr(source_donor, "urn", "") or ""
        )
    if donation.donor_source == "data_file" and donation.data_file_donor is not None:
        return donation.data_file_donor.urn or getattr(donation.donor, "urn", "") or ""
    if donation.donor_id and donation.donor is not None:
        return donation.donor.urn or ""
    if donation.data_file_donor_id and donation.data_file_donor is not None:
        return donation.data_file_donor.urn or ""
    return ""


def get_active_donor(donation: Donation) -> object:
    """Return the donor attached to *donation*, falling back to a safe stub.

    Args:
        donation: Donation instance.

    Returns:
        A donor model instance or a ``SimpleNamespace`` with empty defaults.
    """
    source_donor = donation.donor or donation.data_file_donor
    donor = donation.system_donor or source_donor
    if donor:
        return SimpleNamespace(
            urn=get_donation_display_urn(donation),
            title=donor.title,
            first_name=donor.first_name,
            last_name=donor.last_name,
            email=donor.email,
            phone=donor.phone,
            address_line1=donor.address_line1,
            address_line2=donor.address_line2,
            city=donor.city,
            county=donor.county,
            postcode=donor.postcode,
            country=donor.country,
            gift_aid_declaration=donor.gift_aid_declaration,
            consent_contact=donor.consent_contact,
            no_thank_you=bool(getattr(source_donor, "no_thank_you", False)),
            opt_in_email=donor.opt_in_email,
            opt_in_sms=donor.opt_in_sms,
            opt_in_phone=donor.opt_in_phone,
            opt_in_post=donor.opt_in_post,
            pending_review=bool(
                getattr(donation.system_donor, "pending_review", False)
            ),
        )
    return SimpleNamespace(
        urn="",
        title="",
        first_name="",
        last_name="",
        email="",
        phone="",
        address_line1="",
        address_line2="",
        city="",
        county="",
        postcode="",
        country="",
        gift_aid_declaration=False,
        consent_contact=False,
        no_thank_you=False,
        opt_in_email=False,
        opt_in_sms=False,
        opt_in_phone=False,
        opt_in_post=False,
        pending_review=False,
    )


def get_package_code_value(donation: Donation) -> str:
    """Return the best available package code for QA display.

    Preference order is campaign-specific donor data, stored donation field data,
    then any linked package code relation on the donation.

    Args:
        donation: Donation being reviewed.

    Returns:
        Package code string or an empty string when unavailable.
    """
    data_file_donor = donation.data_file_donor
    if data_file_donor and data_file_donor.package_code:
        return data_file_donor.package_code

    field_data = donation.field_data or {}
    stored_package_code = field_data.get("package_code", "")
    if isinstance(stored_package_code, str) and stored_package_code.strip():
        return stored_package_code.strip()

    linked_package_code = (
        donation.package_codes.order_by("code").values_list("code", flat=True).first()
    )
    return linked_package_code or ""


# ---------------------------------------------------------------------------
# POST → Model update logic
# ---------------------------------------------------------------------------


def _update_card_fields(
    donation: Donation, data: dict[str, object], updated: list[str]
) -> None:
    """Update PCI-DSS card fields, stripping card_last_four to 4 digits."""
    for field in ("card_holder_name", "card_last_four", "card_expiry_date"):
        value = (data.get(field) or "").strip()
        if field == "card_last_four":
            value = value.replace(" ", "")[-4:] if value else ""
        if value != getattr(donation, field):
            setattr(donation, field, value)
            updated.append(field)


def _update_optional_decimal(
    obj: object, data: dict[str, object], field: str, updated: list[str]
) -> None:
    """Parse an optional Decimal POST field and update if changed."""
    raw = data.get(field)
    parsed = (
        parse_decimal(raw, getattr(obj, field, None)) if raw not in {None, ""} else None  # pyright: ignore[reportArgumentType]
    )
    if parsed != getattr(obj, field, None):
        setattr(obj, field, parsed)
        updated.append(field)


_POSTAL_FIELDS = ("postal_order_number", "postal_issuer")


def update_donation_from_post(donation: Donation, data: dict[str, object]) -> list[str]:
    """Apply POST data to a ``Donation`` instance, returning changed field names."""
    updated: list[str] = []

    # Amount / currency / payment method
    amount = parse_decimal(data.get("amount"), donation.amount)  # pyright: ignore[reportArgumentType]
    if amount != donation.amount:
        donation.amount = amount
        updated.append("amount")

    _update_choice_field(donation, data, "currency", Donation.CURRENCY_CHOICES, updated)
    _update_choice_field(
        donation, data, "payment_method", Donation.PAYMENT_METHOD_CHOICES, updated
    )

    # Donation date is editable in QA. Keep existing value for empty/invalid input.
    donation_date = parse_date(data.get("donation_date"))  # pyright: ignore[reportArgumentType]
    if donation_date is not None and donation_date != donation.donation_date:
        donation.donation_date = donation_date
        updated.append("donation_date")

    # Dates & cheque
    _update_text_field(donation, data, "cheque_number", updated)
    _update_date_field(donation, data, "cheque_date", updated)

    # Gift aid
    gift_aid = bool_from_post(data, "gift_aid")
    if gift_aid != donation.gift_aid:
        donation.gift_aid = gift_aid
        updated.append("gift_aid")

    _update_choice_field(
        donation,
        data,
        "donation_frequency",
        Donation.FREQUENCY_CHOICES,
        updated,
        post_key="donation_frequency",
    )

    # Card (PCI-DSS)
    _update_card_fields(donation, data, updated)

    # Direct debit dates
    _update_date_field(donation, data, "direct_debit_start_date", updated)
    _update_date_field(donation, data, "direct_debit_end_date", updated)

    _update_text_field(donation, data, "caf_voucher_number", updated)
    _update_text_field(donation, data, "caf_donor_name", updated)
    _update_optional_decimal(donation, data, "caf_amount", updated)

    for field in _POSTAL_FIELDS:
        _update_text_field(donation, data, field, updated)
    _update_date_field(donation, data, "postal_order_date", updated)

    # Non-financial
    for field in ("non_financial_reason", "non_financial_notes"):
        _update_text_field(donation, data, field, updated)

    return updated


def update_package_code_from_post(
    donation: Donation, data: Mapping[str, object]
) -> list[str]:
    """Persist package code edits from the QA review form.

    Args:
        donation: Donation being reviewed.
        data: POST data.

    Returns:
        Donation fields that need saving after package-code updates.
    """
    updated: list[str] = []
    package_code = (data.get("package_code") or "").strip()  # type: ignore[arg-type]

    field_data = (
        donation.field_data.copy() if isinstance(donation.field_data, dict) else {}
    )
    stored_package_code = field_data.get("package_code", "")
    stored_package_code = (
        stored_package_code.strip() if isinstance(stored_package_code, str) else ""
    )
    if package_code:
        if stored_package_code != package_code:
            field_data["package_code"] = package_code
            donation.field_data = field_data
            updated.append("field_data")
    elif "package_code" in field_data:
        field_data.pop("package_code", None)
        donation.field_data = field_data
        updated.append("field_data")

    data_file_donor = donation.data_file_donor
    if data_file_donor and data_file_donor.package_code != package_code:
        data_file_donor.package_code = package_code
        data_file_donor.save(update_fields=["package_code"])

    matched_package = None
    if package_code:
        matched_package = (
            donation.campaign.package_codes.filter(code__iexact=package_code)
            .only("id")
            .first()
        )

    desired_ids = [matched_package.id] if matched_package else []
    current_ids = list(donation.package_codes.values_list("id", flat=True))
    if current_ids != desired_ids:
        donation.package_codes.set(desired_ids)

    return updated


# Re-exported so existing callers (tests, QA review) continue to import from
# this module. The canonical implementation lives in ``donors.updates`` so
# phone-intake (which cannot import from ``custom_admin``) can share it.
from donors.updates import (  # noqa: E402
    update_donor_from_post as update_donor_from_post,
)

# ---------------------------------------------------------------------------
# Internal micro-helpers
# ---------------------------------------------------------------------------


def _update_text_field(
    obj: object,
    data: dict[str, object],
    field: str,
    updated: list[str],
    *,
    post_key: str | None = None,
) -> None:
    """Set a stripped text field if it differs from the current value."""
    value = (data.get(post_key or field) or "").strip()
    if value != getattr(obj, field, ""):
        setattr(obj, field, value)
        updated.append(field)


def _update_bool_field(
    obj: object,
    data: dict[str, object],
    field: str,
    updated: list[str],
    *,
    post_key: str | None = None,
) -> None:
    """Set a boolean field from POST data if it differs."""
    value = bool_from_post(data, post_key or field)
    if hasattr(obj, field) and value != getattr(obj, field):
        setattr(obj, field, value)
        updated.append(field)


def _update_date_field(
    obj: object,
    data: dict[str, object],
    field: str,
    updated: list[str],
) -> None:
    """Set a date field parsed from POST data if it differs."""
    value = parse_date(data.get(field))  # pyright: ignore[reportArgumentType]
    if value != getattr(obj, field):
        setattr(obj, field, value)
        updated.append(field)


def _update_choice_field(
    obj: object,
    data: dict[str, object],
    field: str,
    choices: list[tuple[str, str]],
    updated: list[str],
    *,
    post_key: str | None = None,
) -> None:
    """Set a choice field if the POST value is valid and differs from current."""
    key = post_key or field
    value = (data.get(key) or "").strip()
    valid = {c[0] for c in choices}
    if value in valid and value != getattr(obj, field):
        setattr(obj, field, value)
        updated.append(field)
