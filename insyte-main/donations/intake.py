"""Phone-call donation intake — service layer.

This module is the parallel of ``scans.scan_processing_donations`` for the
phone-intake path. Where the scan pipeline creates ``Donation`` rows from
OCR-derived ``ScanPlaceholder`` records, this module creates them from
operator-typed form data captured during a phone call.

The two paths diverge only in *how the input data is collected*; everything
downstream — QA review, payment capture, letter generation, banking
reconciliation — is unchanged. Phone donations land in the same
``DonationBatch`` model, traverse the same QA queue, and trigger the same
post-approval cascades.

One-batch-per-call
------------------
Each phone call creates its own ``DonationBatch`` so card-eligible
donations can auto-approve their batch on a clean Stripe capture and
skip the QA queue entirely. The batch name carries a microsecond
timestamp so the existing ``DonationBatch`` ``UniqueConstraint`` on
``(campaign, batch_name)`` keeps each row distinct::

    Phone — {operator_username} — {YYYY-MM-DDTHH:MM:SS.ffffff+TZ} — {campaign.name}

Cheque / cash / DD / 3DS-pending donations land in their own
single-donation batch and still flow through the existing QA approval.

field_data marker
-----------------
Phone donations stamp ``field_data["intake_method"] = "phone"`` so
downstream readers (reporting, audit, redaction) can distinguish them
from OCR donations. The CVV redaction guard in
``payments/batch_payment.py`` already short-circuits when no
``ScanPlaceholder`` exists, so phone donations cleanly skip redaction
without any code change there.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date as date_cls
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypedDict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from donations.bacs_validation import (
    REASON_INVALID_FORMAT,
    normalize_account_number,
    normalize_sort_code,
    validate_bacs,
)
from donations.models import Donation, DonationBatch
from donors.updates import (
    PHONE_INTAKE_EDITABLE_FIELDS,
    DonorVanished,
    _lock_self,
    apply_donor_field_updates,
)

if TYPE_CHECKING:
    from campaigns.models import Campaign
    from core.models import User
    from donors.models import DataFileDonor, Donor, SystemDonor

logger = logging.getLogger(__name__)

# Donor-source values match Donation.DONOR_SOURCE_CHOICES.
DONOR_SOURCE_HOUSE_FILE = "house_file"
DONOR_SOURCE_DATA_FILE = "data_file"

# Donor-match status surfaced in field_data["donor_match_status"]. Used by
# reporting and the QA dashboard to highlight donations whose donor was
# typed in by an operator vs. matched against an existing record.
MATCH_STATUS_EXACT = "exact"
MATCH_STATUS_FUZZY = "fuzzy"
MATCH_STATUS_MANUAL = "manual"
MATCH_STATUS_NEW = "new"

# Versioned mandate consent text shown to the operator and read aloud to
# the donor. The version + a SHA-256 hash of the *rendered* text (with
# donor name / amount / frequency substituted in) lands in
# ``field_data["dd_mandate_consent"]`` so audits can prove what the
# donor was told. Bumping the version when scheme rules change preserves
# old mandates as still-valid under their original text version.
PHONE_INTAKE_DD_MANDATE_CONSENT_VERSION = "v1"
PHONE_INTAKE_DD_MANDATE_CONSENT_TEMPLATE = (
    "I authorise {client_name} to send instructions to my bank or "
    "building society to debit my account in accordance with the "
    "Direct Debit Guarantee. I confirm that I am the account holder "
    "and the only person required to authorise debits from this "
    "account. The first payment of £{amount} will be collected on or "
    "after {start_date}, recurring {frequency}."
)


def render_mandate_consent_text(
    *,
    client_name: str,
    amount: Decimal,
    start_date: date_cls | None,
    frequency: str,
) -> str:
    """Render the operator-read mandate consent text for a specific donor.

    The substituted text is what the operator reads aloud to the donor;
    a SHA-256 hash lands in field_data so audits can prove the exact
    paragraph, including donor-specific values, that was spoken.
    """
    return PHONE_INTAKE_DD_MANDATE_CONSENT_TEMPLATE.format(
        client_name=client_name or "the charity",
        amount=f"{amount:.2f}",
        start_date=start_date.isoformat() if start_date else "the agreed date",
        frequency=frequency or "as agreed",
    )


class PhoneDonationPayload(TypedDict, total=False):
    """Operator-supplied phone-intake form data.

    Required keys:

    * ``amount`` — Decimal donation amount (>= 0).
    * ``payment_method`` — one of ``Donation.PAYMENT_METHOD_*`` values.

    Optional keys depend on the chosen payment method. Date fields (e.g.
    ``cheque_date``) may be ``None`` to defer to banking later.
    """

    amount: Decimal
    currency: str
    payment_method: str
    donation_date: date_cls | None
    gift_aid: bool
    donation_frequency: str

    # Donor linkage (one of these is set; the other two are None).
    donor_source: str  # "house_file" or "data_file"

    # Payment-specific fields.
    cheque_number: str
    cheque_date: date_cls | None
    caf_voucher_number: str
    caf_amount: Decimal
    postal_order_number: str
    postal_order_date: date_cls | None
    sort_code: str
    account_number: str
    direct_debit_start_date: date_cls | None
    card_holder_name: str
    card_last_four: str
    card_expiry_date: str

    # Mandate consent block (Phase 3, populated for direct_debit only).
    # Required key: ``verbatim_read_aloud: bool`` — the server rejects
    # the donation when this is missing/falsy.
    dd_mandate_consent: dict[str, Any]

    # When True, allow saving DD even when format validation fails
    # (banking will manually verify the unusual account on receipt).
    # Forces qa_status=flagged.
    bacs_validation_overridden: bool

    # Match metadata for field_data audit.
    donor_match_status: str

    # Non-financial intake (donor-update or in-kind / volunteering / legacy).
    non_financial_reason: str
    non_financial_notes: str

    # When set, gets merged into ``field_data`` so QA review can distinguish
    # operator-driven donor updates from genuine non-financial donations.
    intake_subtype: str


PHONE_BATCH_NAME_PREFIX = "Phone — "


def _format_phone_batch_name(
    *, operator: User, campaign: Campaign, now: datetime
) -> str:
    """Return a unique phone-intake batch name for a single call.

    Microsecond precision on *now* + the existing
    :class:`DonationBatch` ``UniqueConstraint(["campaign", "batch_name"])``
    keeps each call's batch distinct — operators submit donations far
    slower than 1/microsecond so collisions are not a realistic concern.
    """
    return (
        f"{PHONE_BATCH_NAME_PREFIX}{operator.username} — "
        f"{now.isoformat()} — {campaign.name}"
    )


def create_phone_intake_batch(
    *, operator: User, campaign: Campaign, now: datetime | None = None
) -> DonationBatch:
    """Create a fresh ``DonationBatch`` for a single phone call.

    Every call gets its own batch so card-eligible donations can
    auto-approve the batch on a clean charge (see
    :func:`apply_phone_intake_auto_approval`) and skip the QA queue
    entirely. Cheque / cash / DD / 3DS-pending donations also each get
    their own batch and still flow through the existing QA approval.

    Args:
        operator: The User taking the phone call.
        campaign: The Campaign the donation belongs to.
        now: Override the timestamp baked into the batch name (defaults
            to ``timezone.now()``). Test seam.

    Returns:
        The newly created DonationBatch in ``STATUS_PENDING_QA``.
    """
    if now is None:
        now = timezone.now()

    batch_name = _format_phone_batch_name(operator=operator, campaign=campaign, now=now)

    return DonationBatch.objects.create(
        campaign=campaign,
        batch_name=batch_name,
        created_by=operator,
        default_payment_method="",
        default_currency="GBP",
        status=DonationBatch.STATUS_PENDING_QA,
    )


def _payment_specific_fields_from_payload(
    payload: PhoneDonationPayload,
) -> dict[str, Any]:
    """Extract payment-method-specific fields from an operator-supplied payload.

    Mirrors :func:`scans.scan_processing_donations._payment_specific_fields`
    but reads from operator input rather than OCR-extracted data. Fields
    not relevant to the chosen ``payment_method`` are zero/empty so the
    Donation row is consistently shaped regardless of intake source.
    """
    method = payload.get("payment_method", "")

    fields: dict[str, Any] = {
        "cheque_number": "",
        "cheque_date": None,
        "card_holder_name": "",
        "card_last_four": "",
        "card_expiry_date": "",
        "caf_voucher_number": "",
        "caf_amount": Decimal("0.00"),
        "postal_order_number": "",
        "postal_order_date": None,
        "sort_code": "",
        "account_number": "",
        "direct_debit_start_date": None,
        "non_financial_reason": "",
        "non_financial_notes": "",
    }

    if method == Donation.PAYMENT_METHOD_CARD:
        fields.update(
            {
                "card_holder_name": payload.get("card_holder_name", ""),
                "card_last_four": (payload.get("card_last_four") or "")[:4],
                "card_expiry_date": payload.get("card_expiry_date", ""),
            }
        )
    elif method == Donation.PAYMENT_METHOD_DIRECT_DEBIT:
        fields.update(
            {
                "card_holder_name": payload.get("card_holder_name", ""),
                "sort_code": normalize_sort_code(payload.get("sort_code", "")),
                "account_number": normalize_account_number(
                    payload.get("account_number", "")
                ),
                "direct_debit_start_date": payload.get("direct_debit_start_date"),
            }
        )
    elif method == Donation.PAYMENT_METHOD_CHEQUE:
        fields.update(
            {
                "cheque_number": payload.get("cheque_number", ""),
                "cheque_date": payload.get("cheque_date"),
            }
        )
    elif method == Donation.PAYMENT_METHOD_CAF:
        fields.update(
            {
                "caf_voucher_number": payload.get("caf_voucher_number", ""),
                "caf_amount": payload.get("caf_amount") or Decimal("0.00"),
            }
        )
    elif method == Donation.PAYMENT_METHOD_POSTAL_ORDER:
        fields.update(
            {
                "postal_order_number": payload.get("postal_order_number", ""),
                "postal_order_date": payload.get("postal_order_date"),
            }
        )
    elif method == Donation.PAYMENT_METHOD_NON_FINANCIAL:
        fields.update(
            {
                "non_financial_reason": payload.get("non_financial_reason", ""),
                "non_financial_notes": payload.get("non_financial_notes", ""),
            }
        )

    return fields


def _resolve_donor_links(
    donor: Donor | DataFileDonor | SystemDonor | None,
) -> dict[str, Any]:
    """Resolve which of the three donor FK fields the donation should set.

    Returns a dict with ``donor``, ``data_file_donor`` and ``system_donor``
    keys (any of which may be ``None``) plus a ``donor_source`` value
    suitable for ``Donation.donor_source``.
    """
    from donors.models import DataFileDonor, Donor, SystemDonor

    links: dict[str, Any] = {
        "donor": None,
        "data_file_donor": None,
        "system_donor": None,
        "donor_source": DONOR_SOURCE_HOUSE_FILE,
    }
    if isinstance(donor, SystemDonor):
        links["system_donor"] = donor
        links["donor_source"] = DONOR_SOURCE_HOUSE_FILE
    elif isinstance(donor, Donor):
        links["donor"] = donor
        links["donor_source"] = DONOR_SOURCE_HOUSE_FILE
    elif isinstance(donor, DataFileDonor):
        links["data_file_donor"] = donor
        links["donor_source"] = DONOR_SOURCE_DATA_FILE
    return links


def _build_field_data(
    *, operator: User, payload: PhoneDonationPayload
) -> dict[str, Any]:
    """Build the ``field_data`` JSON marker for a phone-intake donation.

    UUIDs (e.g. ``operator.id``) are stringified so the dict can survive
    Django's JSONField serialization without a custom encoder.
    """
    data: dict[str, Any] = {
        "intake_method": "phone",
        "intake_operator_id": str(operator.id),
        "intake_operator_username": operator.username,
        "captured_at": timezone.now().isoformat(),
        "donor_match_status": payload.get("donor_match_status", MATCH_STATUS_MANUAL),
    }
    subtype = (payload.get("intake_subtype") or "").strip()
    if subtype:
        data["intake_subtype"] = subtype
    return data


def _resolve_qa_status(
    *,
    payload: PhoneDonationPayload,
    donor: Donor | DataFileDonor | SystemDonor | None,
    bacs_override: bool = False,
) -> str:
    """Decide the QA status for a freshly-created phone donation.

    Phone donations land at ``pending`` by default. They flip to
    ``flagged`` only when the operator-supplied data has a known issue
    that demands reviewer attention before approval — currently:

    * Amount is missing or non-positive (operator skipped a required field).
    * The matched donor is a SystemDonor with ``pending_review=True``
      (auto-created by the scan pipeline; QA must verify identity before
      money flows).
    * Direct-Debit, in any form: until the VocaLink modulus table ships,
      every DD intake is flagged so QA double-checks the operator-typed
      sort code + account number — including the operator-override path,
      where a non-standard account specifically demands a second pair of
      eyes before the mandate is banked.

    The ``bacs_override`` argument is unused while DD is unconditionally
    flagged. Once the modulus check ships, DD will branch back to
    ``pending`` for clean validations and override will be the only
    remaining flag-trigger — the parameter is preserved on the signature
    so that flip is a one-liner.
    """
    from donors.models import SystemDonor

    _ = bacs_override  # see docstring — preserved for the post-modulus flip.

    payment_method = payload.get("payment_method", "")

    # Non-financial intake intentionally has amount=0 (donor-update) or
    # represents an in-kind / volunteering record. The "amount missing"
    # flag does not apply here.
    if payment_method != Donation.PAYMENT_METHOD_NON_FINANCIAL:
        amount = payload.get("amount") or Decimal("0.00")
        if amount <= Decimal("0.00"):
            return Donation.QA_STATUS_FLAGGED

    if isinstance(donor, SystemDonor) and donor.pending_review:
        return Donation.QA_STATUS_FLAGGED

    if payment_method == Donation.PAYMENT_METHOD_DIRECT_DEBIT:
        return Donation.QA_STATUS_FLAGGED

    return Donation.QA_STATUS_PENDING


def is_phone_intake_auto_approve_eligible(
    *,
    payload: PhoneDonationPayload,
    donor: Donor | DataFileDonor | SystemDonor | None,
) -> bool:
    """Whether this phone donation may skip QA on a clean charge.

    Auto-approval applies only to card donations whose pre-charge state
    is otherwise QA-clean: positive amount, non-DD method, donor not
    flagged for review. The actual ``qa_status`` flip happens later, only
    after Stripe confirms the charge settled — see
    :func:`donations.phone_intake_views.phone_intake_charge`.

    Returning ``False`` keeps the donation on the existing QA queue.
    """
    if payload.get("payment_method") != Donation.PAYMENT_METHOD_CARD:
        return False
    qa_status = _resolve_qa_status(payload=payload, donor=donor)
    return qa_status == Donation.QA_STATUS_PENDING


def is_donation_auto_approve_eligible(donation: Donation) -> bool:
    """Whether a saved phone Donation row may auto-approve post-charge.

    Mirror of :func:`is_phone_intake_auto_approve_eligible` for the
    webhook recovery path: when a 3DS PaymentIntent settles
    asynchronously, the original payload is gone and we must reconstruct
    the eligibility check from saved fields. The rules match — card
    method, positive amount, donor not pending-review, not currently
    flagged.

    Used by :func:`core.tasks.process_stripe_webhook` to retro-approve
    a phone donation when the donor completes SCA off-call.
    """
    from donors.models import SystemDonor

    if donation.field_data.get("intake_method") != "phone":
        return False
    if donation.payment_method != Donation.PAYMENT_METHOD_CARD:
        return False
    if donation.amount <= Decimal("0.00"):
        return False
    if donation.qa_status != Donation.QA_STATUS_PENDING:
        return False
    has_pending_review_donor = (
        donation.system_donor_id is not None
        and SystemDonor.objects.filter(
            pk=donation.system_donor_id, pending_review=True
        ).exists()
    )
    return not has_pending_review_donor


def apply_phone_intake_auto_approval(donation: Donation, *, note: str) -> None:
    """Promote a phone-intake Donation **and its parent batch** to approved.

    With the one-batch-per-call model, a clean card charge auto-approves
    both the donation and its parent batch in the same atomic step. The
    batch ``post_save`` signal (``core/signals.py``) sees ``status`` in
    update_fields and queues the existing ``on_batch_approved_task`` on
    commit, which writes the HMRC Gift Aid CSV — same approval pipeline
    a QA reviewer would trigger by clicking Approve Batch, just without
    the human in the loop.

    The audit middleware captures the actor (operator user from the
    request, or system/None for webhook-driven calls) via its contextvar.

    Args:
        donation: The Donation row to promote. Caller is responsible for
            ensuring eligibility (see
            :func:`is_phone_intake_auto_approve_eligible` /
            :func:`is_donation_auto_approve_eligible`).
        note: Text recorded in ``qa_notes`` so retrospective audit
            sampling can distinguish auto-approvals from reviewer
            approvals (e.g. "Auto-approved at phone intake — card
            captured live with donor").
    """
    with transaction.atomic():
        donation.qa_status = Donation.QA_STATUS_APPROVED
        donation.qa_notes = note
        field_data = dict(donation.field_data or {})
        field_data["auto_approved_at"] = timezone.now().isoformat()
        donation.field_data = field_data
        donation.save(
            update_fields=["qa_status", "qa_notes", "field_data", "updated_at"]
        )

        batch = donation.batch
        if batch is None:
            return

        # Idempotency: read the persisted status (not the cached FK) so a
        # racing path that already approved this batch doesn't make us
        # re-save it — that would re-fire the ``status`` post_save signal
        # and queue a duplicate ``on_batch_approved_task`` (and a duplicate
        # status email). Mirrors :func:`_commit_batch_status` in qa_review.
        persisted_status = (
            DonationBatch.objects.filter(pk=batch.pk)
            .values_list("status", flat=True)
            .first()
        )
        if persisted_status == DonationBatch.STATUS_APPROVED:
            return

        batch.status = DonationBatch.STATUS_APPROVED  # type: ignore[assignment]
        batch.reviewed_at = timezone.now()
        batch.review_notes = "Auto-approved at phone intake — card captured live."
        batch.save(
            update_fields=[
                "status",
                "reviewed_at",
                "review_notes",
                "updated_at",
            ]
        )


def create_phone_donation(
    *,
    operator: User,
    campaign: Campaign,
    batch: DonationBatch,
    donor: Donor | DataFileDonor | SystemDonor | None,
    payload: PhoneDonationPayload,
    donor_updates: dict[str, object] | None = None,
) -> Donation:
    """Create a Donation row from operator-supplied phone-intake data.

    Mirrors the discipline of
    :func:`scans.scan_processing_donations.create_donation_from_placeholder`
    but without OCR/placeholder fields. The donation lands in *batch*
    (which is required) at ``qa_status=pending`` (or ``flagged`` when a
    server-side validation flag fires).

    Args:
        operator: The User taking the call.
        campaign: The Campaign the donation belongs to. Must equal
            ``batch.campaign``.
        batch: The DonationBatch the donation joins. Typically obtained
            from :func:`create_phone_intake_batch`.
        donor: The matched donor (any of Donor / DataFileDonor /
            SystemDonor) or ``None`` if no donor is yet linked.
        payload: Operator-supplied form data.
        donor_updates: Optional whitelisted donor-edit payload (keys follow
            the ``donor_<field>`` pattern, already filtered through
            :func:`donations.phone_intake_views._extract_donor_update_payload`).
            When non-empty and a donor is linked, the donor row is
            row-locked and mutated via
            :func:`donors.updates.apply_donor_field_updates` inside the
            same atomic block as the donation row, so a failure in either
            step rolls back both.

    Returns:
        The newly created Donation row.

    Raises:
        DonorVanished: ``donor_updates`` was supplied but the donor row
            was deleted between page-load and submit.
    """
    if batch.campaign_id != campaign.id:
        raise ValueError(
            "Phone donation batch must belong to the same campaign as the donation",
        )

    payment_specific = _payment_specific_fields_from_payload(payload)
    field_data = _build_field_data(operator=operator, payload=payload)

    # Direct Debit: format-validate sort code + account number, capture
    # mandate consent in field_data, optionally allow operator override
    # (which forces qa_status=flagged so a reviewer double-checks).
    bacs_override = bool(payload.get("bacs_validation_overridden", False))
    if payload.get("payment_method") == Donation.PAYMENT_METHOD_DIRECT_DEBIT:
        ok, reason = validate_bacs(
            payment_specific["sort_code"],
            payment_specific["account_number"],
        )
        if not ok and reason == REASON_INVALID_FORMAT and not bacs_override:
            raise ValidationError(
                "Sort code must be 6 digits and account number must be 6-10 "
                "digits. Re-enter the details or tick 'Force save' if the "
                "donor's account is non-standard.",
            )
        # Either format passed (warning) or operator overrode — either
        # way, record the validation outcome on field_data so QA sees it.
        field_data["bacs_validation"] = {"reason": reason, "overridden": bacs_override}

        # Mandate consent: required for direct_debit. The operator must
        # tick the "verbatim_read_aloud" flag client-side; the server
        # also rejects donations missing the consent block.
        consent_block = payload.get("dd_mandate_consent") or {}
        if not consent_block.get("verbatim_read_aloud"):
            raise ValidationError(
                "Mandate consent is required: confirm you read the mandate "
                "text verbatim to the donor before saving.",
            )
        rendered = render_mandate_consent_text(
            client_name=getattr(campaign.client, "name", ""),
            amount=payload.get("amount") or Decimal("0.00"),
            start_date=payload.get("direct_debit_start_date"),
            frequency=payload.get("donation_frequency", ""),
        )
        field_data["dd_mandate_consent"] = {
            "recorded_at": timezone.now().isoformat(),
            "consent_text_version": PHONE_INTAKE_DD_MANDATE_CONSENT_VERSION,
            "consent_text_rendered_hash": hashlib.sha256(
                rendered.encode("utf-8")
            ).hexdigest(),
            "operator_id": str(operator.id),
            "operator_username": operator.username,
            "verbatim_read_aloud": True,
        }

    qa_status = _resolve_qa_status(
        payload=payload, donor=donor, bacs_override=bacs_override
    )

    amount = payload.get("amount") or Decimal("0.00")
    if amount < Decimal("0.00"):
        amount = Decimal("0.00")

    with transaction.atomic():
        # When the operator supplied donor edits, row-lock the donor and
        # apply them before creating the donation. The whole compound
        # (donor mutation + donation row) is atomic — if either step
        # fails, neither persists. Mirrors the pattern
        # ``process_phone_non_financial_intake`` uses for the
        # non-financial flow.
        active_donor: Donor | DataFileDonor | SystemDonor | None = donor
        if donor is not None and donor_updates:
            try:
                active_donor = _lock_self(type(donor).objects).get(pk=donor.pk)
            except type(donor).DoesNotExist as exc:
                raise DonorVanished(
                    f"{type(donor).__name__} {donor.pk} was deleted before submit",
                ) from exc
            apply_donor_field_updates(
                active_donor,
                donor_updates,
                editable_fields=PHONE_INTAKE_EDITABLE_FIELDS,
            )

        donor_links = _resolve_donor_links(active_donor)

        donation = Donation.objects.create(
            campaign=campaign,
            batch=batch,
            filled_by=operator,
            donor_source=donor_links["donor_source"],
            donor=donor_links["donor"],
            data_file_donor=donor_links["data_file_donor"],
            system_donor=donor_links["system_donor"],
            amount=amount,
            currency=payload.get("currency", "GBP"),
            payment_method=payload.get("payment_method", ""),
            donation_date=payload.get("donation_date") or timezone.now().date(),
            gift_aid=bool(payload.get("gift_aid", False)),
            donation_frequency=payload.get("donation_frequency", ""),
            cheque_number=payment_specific["cheque_number"],
            cheque_date=payment_specific["cheque_date"],
            card_holder_name=payment_specific["card_holder_name"],
            card_last_four=payment_specific["card_last_four"],
            card_expiry_date=payment_specific["card_expiry_date"],
            caf_voucher_number=payment_specific["caf_voucher_number"],
            caf_amount=payment_specific["caf_amount"],
            postal_order_number=payment_specific["postal_order_number"],
            postal_order_date=payment_specific["postal_order_date"],
            sort_code=payment_specific["sort_code"],
            account_number=payment_specific["account_number"],
            direct_debit_start_date=payment_specific["direct_debit_start_date"],
            non_financial_reason=payment_specific["non_financial_reason"],
            non_financial_notes=payment_specific["non_financial_notes"],
            qa_status=qa_status,
            field_data=field_data,
        )

    return donation


def process_phone_non_financial_intake(
    *,
    operator: User,
    campaign: Campaign,
    batch: DonationBatch,
    donor: Donor | DataFileDonor | SystemDonor | None,
    donation_payload: PhoneDonationPayload,
    donor_update_payload: dict[str, object],
) -> Donation:
    """Orchestrate a non-financial donor-update phone intake.

    Mutates the linked donor row and creates a non-financial Donation row
    in a single ``transaction.atomic()`` block. A failure in either step
    rolls back both — no orphan rows.

    The created Donation is shaped so the QA-approval cascades downstream
    cannot misinterpret it:

    * ``amount = 0`` and ``gift_aid = False`` keep the row out of the HMRC
      Gift Aid CSV (which filters ``gift_aid=True``).
    * ``letter_status = "skipped"`` short-circuits the letter-generation
      queryset, which otherwise has no ``payment_method`` filter and would
      queue a thank-you for what was actually a "marked deceased" event.
    * ``field_data["intake_subtype"] = "donor_update"`` is the marker QA
      uses to render the "Donor Update (phone)" badge and to filter
      non-financial *donor updates* from non-financial *donations*
      (in-kind, volunteering, legacy).

    Raises:
        ValidationError: ``donor`` is None, ``payment_method`` is not
            ``non_financial``, or ``non_financial_reason`` is blank.
        DonorVanished: The donor row was deleted between page-load and
            submit. Views should translate to HTTP 410.
    """
    if donor is None:
        raise ValidationError("Donor required for non-financial intake")
    if donation_payload.get("payment_method") != Donation.PAYMENT_METHOD_NON_FINANCIAL:
        raise ValidationError(
            "process_phone_non_financial_intake only handles "
            "payment_method='non_financial'",
        )
    reason = (donation_payload.get("non_financial_reason") or "").strip()
    if not reason:
        raise ValidationError("non_financial_reason is required")

    # Force-set fields that protect downstream pipelines from misinterpreting
    # the row. The view layer also enforces these; defence in depth.
    donation_payload["amount"] = Decimal("0.00")
    donation_payload["gift_aid"] = False
    donation_payload["non_financial_reason"] = reason
    donation_payload["intake_subtype"] = "donor_update"

    with transaction.atomic():
        try:
            locked_donor = _lock_self(type(donor).objects).get(pk=donor.pk)
        except type(donor).DoesNotExist as exc:
            raise DonorVanished(
                f"{type(donor).__name__} {donor.pk} was deleted before submit",
            ) from exc

        apply_donor_field_updates(
            locked_donor,
            donor_update_payload,
            editable_fields=PHONE_INTAKE_EDITABLE_FIELDS,
        )

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=locked_donor,
            payload=donation_payload,
        )

        # Skip the thank-you letter pipeline for donor-update events. The
        # letter queryset filters on ``letter_status`` ahead of any
        # ``payment_method`` check, so this is the canonical opt-out.
        donation.letter_status = "skipped"
        donation.letter_void_reason = "Non-financial donor update"
        donation.letter_voided_at = timezone.now()
        donation.save(
            update_fields=[
                "letter_status",
                "letter_void_reason",
                "letter_voided_at",
            ],
        )

    return donation


def create_system_donor_inline(
    *,
    campaign: Campaign,
    operator: User,
    payload: dict[str, Any],
) -> SystemDonor:
    """Create a new SystemDonor on the spot during a phone call.

    The operator already searched (extended donor_search runs against
    SystemDonor first, then Donor, then DataFileDonor) and got no hits
    before clicking "Create new donor", so we don't repeat fuzzy matching
    here — we go straight to ``SystemDonor.objects.create()`` with
    ``pending_review=True`` so the back office can verify the new donor
    later, mirroring how scan-pipeline-created donors are flagged for
    review.

    Args:
        campaign: Campaign whose client owns the new SystemDonor.
        operator: User creating the donor (recorded as ``created_by``).
        payload: Form data with at minimum ``first_name``, ``last_name``;
            optionally ``title``, ``email``, ``phone``, ``postcode``,
            address lines, consent flags, ``external_urn``.

    Returns:
        The newly created or upserted SystemDonor.
    """
    from donors.models import SystemDonor

    # The scan pipeline's ``upsert_system_donor_from_source`` expects a
    # Donor, DataFileDonor or ScanPlaceholder as its source — we have a
    # raw operator-typed payload, none of which fit. Calling the
    # SystemDonor manager directly with a normalised payload and
    # ``pending_review=True`` is the most honest mapping; the back office
    # then verifies the new donor through the same review queue used for
    # scan-auto-created donors.
    client = campaign.client

    donor = SystemDonor.objects.create(
        client=client,
        external_urn=payload.get("external_urn", ""),
        title=payload.get("title", ""),
        first_name=payload.get("first_name", "Unknown"),
        last_name=payload.get("last_name", "Unknown"),
        email=payload.get("email", ""),
        phone=payload.get("phone", ""),
        address_line1=payload.get("address_line1", ""),
        address_line2=payload.get("address_line2", ""),
        city=payload.get("city", ""),
        county=payload.get("county", ""),
        postcode=payload.get("postcode", ""),
        country=payload.get("country", "United Kingdom"),
        consent_contact=bool(payload.get("consent_contact", False)),
        opt_in_email=bool(payload.get("opt_in_email", False)),
        opt_in_sms=bool(payload.get("opt_in_sms", False)),
        opt_in_phone=bool(payload.get("opt_in_phone", False)),
        opt_in_post=bool(payload.get("opt_in_post", False)),
        gift_aid_declaration=bool(payload.get("gift_aid_declaration", False)),
        pending_review=True,
        created_by=operator,
        source_snapshot={
            "intake_method": "phone",
            "captured_at": timezone.now().isoformat(),
        },
    )
    return donor
