"""QA Review views for donation batch verification workflow.

This module provides QA team with interfaces to:
- View all batches pending review
- Review batch donations with edit capability
- Approve/reject/request re-check for batches
- Track QA workflow status

Helpers, decorators, and data-update utilities live in
:mod:`custom_admin.views.qa_helpers`.
"""

import contextlib
import json
import logging
from collections.abc import Mapping
from typing import Any, cast

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, Q, QuerySet, Sum
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from audit.utils import log_request_action
from campaigns.models import Campaign
from clients.models import Client
from core.metrics import record_qa_donation_approved
from core.models import User
from core.pagination import paginate_queryset
from core.storage_helpers import normalize_media_storage_name, open_media_storage_file
from core.utils import restore_session_filters
from donations.models import Donation, DonationBatch
from donors.models import DataFileDonor, Donor, SystemDonor
from donors.updates import (
    _lock_self,
    apply_donor_contact_status,
)
from donors.updates import (
    _supports_row_locks as _supports_row_locks,  # re-export for monkey-patching tests
)
from notifications.models import Notification
from responsehandling.permissions import (
    has_permission_or_is_staff,
    is_authenticated_and_is_staff,
)
from scans.donation_scan import DonationScanService
from scans.models import ScanPlaceholder
from scans.scan_processing_donors import upsert_system_donor_from_source

from .qa_utils import (
    get_active_donor,
    get_donation_display_urn,
    get_package_code_value,
    qa_access_required,
    update_donation_from_post,
    update_donor_from_post,
    update_package_code_from_post,
)
from .utils import send_hgv_notification, send_rejection_notification

logger = logging.getLogger(__name__)

_CARD_PAYMENT_METHODS = {"card"}


type _DonorModel = type[Donor] | type[DataFileDonor] | type[SystemDonor]


_ATTENTION_QA_STATUSES = frozenset(
    {Donation.QA_STATUS_PENDING, Donation.QA_STATUS_FLAGGED}
)

# QA dashboard stats use a versioned cache key so batch-status writers can
# invalidate readers without ever calling ``cache.delete``. On every state
# change ``_bump_dashboard_stats_version`` increments a counter; the dashboard
# reads ``f"qa_dashboard_stats:v{counter}"``. This avoids a thundering-herd
# stampede when many reviewers reload the dashboard right after an approval —
# only the first request through the lock recomputes; the rest fall back to
# the previously cached value (or fresh stats if the cache is cold).
_QA_DASHBOARD_STATS_VERSION_KEY = "qa_dashboard_stats:version"
_QA_DASHBOARD_STATS_TTL_SECONDS = 300
_QA_DASHBOARD_STATS_LOCK_TTL_SECONDS = 30

_DONOR_SOURCE_CONFIDENCE_FIELDS = frozenset(
    {
        "title",
        "donor_name",
        "address_line1",
        "address_line2",
        "city",
        "county",
        "postcode",
        "email",
        "phone",
        "email_consent",
        "sms_consent",
        "phone_consent",
        "post_consent",
    }
)

_AUTO_GENERATED_QA_NOTE_PREFIXES = (
    "Auto-created from OCR scan processing",
    "Donor details update from OCR scan processing",
)

_QA_REQUIRED_FIELDS = (
    ("donor_title", "Title"),
    ("donor_first_name", "First Name"),
    ("donor_last_name", "Last Name"),
    ("package_code", "Package Code"),
    ("donor_address_line1", "Address Line 1"),
    ("donor_postcode", "Postcode"),
)


# ---------------------------------------------------------------------------
# Reviewer claim ("soft lock") on a DonationBatch
# ---------------------------------------------------------------------------


def _claim_reviewer_lock(
    batch_id: int, user: User
) -> tuple[DonationBatch, User | None]:
    """Atomically claim or refresh the QA reviewer lock for *batch_id*.

    Wraps the ``SELECT ... FOR UPDATE`` and conditional update in a single
    ``transaction.atomic`` block so two reviewers opening the same batch in
    parallel can never both believe they hold it. The lock is granted when
    nobody currently holds it, the existing claim has aged past
    :data:`donations.models.REVIEWER_LOCK_TTL`, or the same user is
    re-opening (we just refresh the timestamp).

    Args:
        batch_id: Primary key of the batch the reviewer is opening.
        user: The reviewer requesting the claim.

    Returns:
        ``(batch, conflicting_user)`` — ``conflicting_user`` is ``None`` when
        the caller now holds the lock; otherwise it is the live holder.
    """
    with transaction.atomic():
        # ``select_related`` on the *nullable* ``reviewer_locked_by`` FK emits
        # a LEFT OUTER JOIN, and Postgres rejects ``FOR UPDATE`` on the
        # nullable side of an outer join. Lock only the DonationBatch row via
        # ``of=("self",)`` so the join is read-only. The SQLite branch keeps
        # working because ``select_for_update`` is a no-op there.
        batch = (
            _lock_self(DonationBatch.objects)
            .select_related("reviewer_locked_by")
            .get(pk=batch_id)
        )
        now = timezone.now()
        holder = batch.reviewer_locked_by
        same_user = holder is not None and holder.pk == user.pk
        is_active = batch.reviewer_lock_is_active(now=now)

        if is_active and not same_user:
            return batch, holder

        batch.reviewer_locked_by = user  # pyright: ignore[reportAttributeAccessIssue]
        batch.reviewer_locked_at = now
        batch.save(
            update_fields=["reviewer_locked_by", "reviewer_locked_at", "updated_at"]
        )
    return batch, None


def _release_reviewer_lock(batch_id: int, user: User) -> None:
    """Clear the reviewer claim, but only if *user* still owns it.

    Best-effort: a stale lock cleared by the periodic task or already
    re-claimed by another reviewer is left untouched.
    """
    DonationBatch.objects.filter(
        pk=batch_id,
        reviewer_locked_by=user,
    ).update(
        reviewer_locked_by=None,
        reviewer_locked_at=None,
        updated_at=timezone.now(),
    )


def _reviewer_lock_template_context(
    batch: DonationBatch, conflicting_holder: User | None
) -> dict[str, object]:
    """Build template context describing the current reviewer claim."""
    if conflicting_holder is None:
        return {
            "reviewer_lock_conflict": False,
            "reviewer_lock_holder_name": "",
            "reviewer_lock_minutes_ago": 0,
            "reviewer_lock_message": "",
        }
    holder_name = conflicting_holder.get_full_name() or conflicting_holder.username
    locked_at = batch.reviewer_locked_at or timezone.now()
    elapsed = timezone.now() - locked_at
    minutes_ago = max(0, int(elapsed.total_seconds() // 60))
    message = (
        f"This batch is being reviewed by {holder_name} "
        f"(locked {minutes_ago} minute(s) ago) — wait or contact them."
    )
    return {
        "reviewer_lock_conflict": True,
        "reviewer_lock_holder_name": holder_name,
        "reviewer_lock_minutes_ago": minutes_ago,
        "reviewer_lock_message": message,
    }


def _enforce_reviewer_lock_or_raise(batch: DonationBatch, user: User) -> None:
    """Verify *user* still holds an active claim on *batch* before save.

    Raises:
        PermissionDenied: If another reviewer owns the live claim or the
            stored claim has aged out — in either case the in-memory state
            the caller is about to commit was edited without holding the
            current lock and must not be persisted.
    """
    fresh = (
        DonationBatch.objects.select_related("reviewer_locked_by")
        .only(
            "id",
            "reviewer_locked_by",
            "reviewer_locked_at",
        )
        .get(pk=batch.pk)
    )
    holder = fresh.reviewer_locked_by
    if not fresh.reviewer_lock_is_active() or holder is None or holder.pk != user.pk:
        holder_label = (
            (holder.get_full_name() or holder.username)
            if holder is not None
            else "no one"
        )
        raise PermissionDenied(
            "This batch is no longer claimed by you "
            f"(current holder: {holder_label}). "
            "Re-open the batch to acquire a fresh review claim."
        )


def _first_linked_donor(donation: Donation) -> object | None:
    """First non-null donor source linked to the donation.

    Search order matches the QA review form's donor panel:
    house-file Donor → data-file donor → system donor.
    """
    for attr in ("donor", "data_file_donor", "system_donor"):
        target = getattr(donation, attr, None)
        if target is not None:
            return target
    return None


def _current_donor_contact_status(donation: Donation) -> str:
    """Current contact_status of the donor linked to *donation*, for UI prefill."""
    target = _first_linked_donor(donation)
    return getattr(target, "contact_status", "") or Donor.CONTACT_STATUS_NORMAL


def _current_donor_contact_status_reason(donation: Donation) -> str:
    """Current contact_status_reason, for UI prefill."""
    target = _first_linked_donor(donation)
    return getattr(target, "contact_status_reason", "") or ""


def _enqueue_deferred_redactions_for_batch(
    batch: DonationBatch, *, only_non_card: bool = False
) -> None:
    """Fire ``apply_deferred_redaction`` for every redaction-pending placeholder.

    Used from the batch-reject path (apply for everything that still has
    pending coords, since the operator has decided to abandon the batch)
    and the batch-approve path (apply only for non-card donations, since
    card donations defer redaction until their Stripe charge succeeds).

    The post-charge task safety-nets the CVV layer too — a single dispatch
    per placeholder is enough whether the CVV pass already ran or not.

    Args:
        batch: DonationBatch whose donations' coords should fire.
        only_non_card: When True, skip card donations — they are redacted
            from the post-charge hook in ``BatchPaymentService``.
    """
    qs = ScanPlaceholder.objects.filter(
        donation__batch=batch,
        redaction_status__in=[
            ScanPlaceholder.REDACTION_CVV_PENDING,
            ScanPlaceholder.REDACTION_DEFERRED,
        ],
    )
    if only_non_card:
        qs = qs.exclude(donation__payment_method__in=_CARD_PAYMENT_METHODS)
    deferred_ids = list(qs.values_list("id", flat=True))
    if not deferred_ids:
        return
    try:
        from scans.tasks import apply_deferred_redaction_task

        for placeholder_id in deferred_ids:
            apply_deferred_redaction_task.delay(str(placeholder_id))
    except Exception:
        logger.exception(
            "Failed to enqueue apply_deferred_redaction for %d placeholder(s) "
            "in batch %s",
            len(deferred_ids),
            batch.id,
        )


def _enqueue_deferred_redaction_if_pending(donation: Donation) -> None:
    """Fire the deferred redaction Celery task for *donation*'s placeholder.

    No-op when the placeholder doesn't exist or is past the redaction
    pipeline (already COMPLETED). Used from the QA reject and non-card
    approve paths: once the donation's path forward is decided, apply
    saved coords so the readable PAN doesn't sit on R2 indefinitely.

    Errors are swallowed (with logging) so a queue hiccup never bubbles up
    into the calling view.
    """
    placeholder = getattr(donation, "scan_placeholder", None)
    if placeholder is None:
        return
    if placeholder.redaction_status not in (
        ScanPlaceholder.REDACTION_CVV_PENDING,
        ScanPlaceholder.REDACTION_DEFERRED,
    ):
        return
    try:
        from scans.tasks import apply_deferred_redaction_task

        apply_deferred_redaction_task.delay(str(placeholder.id))
    except Exception:
        logger.exception(
            "Failed to enqueue apply_deferred_redaction for placeholder %s "
            "after donation %s reject/approve",
            placeholder.id,
            donation.id,
        )


def _pending_redaction_placeholder(donation: Donation) -> ScanPlaceholder | None:
    """Return the scan placeholder when this donation still needs redaction coords.

    For payment methods configured to require redaction, the operator must
    save blackout coordinates before approval. The actual blackout fires in
    two passes: CVV on auth attempt, post-charge on settlement.

    Returns the placeholder when coords have not yet been saved (status is
    ``PENDING`` / ``IN_PROGRESS`` / ``BLOCKED``) and the method requires it.
    Returns ``None`` once status is ``CVV_PENDING`` (coords saved, both
    passes pending), ``DEFERRED`` (CVV applied, post-charge pending), or
    ``COMPLETED`` (both applied).
    """
    placeholder = getattr(donation, "scan_placeholder", None)
    if placeholder is None:
        return None
    if placeholder.redaction_status in (
        ScanPlaceholder.REDACTION_CVV_PENDING,
        ScanPlaceholder.REDACTION_DEFERRED,
        ScanPlaceholder.REDACTION_COMPLETED,
    ):
        return None
    from scans.scan_redaction import manual_redaction_required_for

    if not manual_redaction_required_for(donation.payment_method or ""):
        return None
    return placeholder


def _is_system_generated_qa_note(value: str) -> bool:
    """Return whether the note text comes from legacy OCR auto-generation."""
    return value.strip().startswith(_AUTO_GENERATED_QA_NOTE_PREFIXES)


def _missing_required_qa_fields(post_data: Mapping[str, object]) -> list[str]:
    """Return missing mandatory QA fields from current review POST payload."""
    missing: list[str] = []
    for post_key, label in _QA_REQUIRED_FIELDS:
        value = post_data.get(post_key, "")
        if not isinstance(value, str):
            value = str(value or "")
        if not value.strip():
            missing.append(label)
    return missing


def _confidence_scores_for_display(
    donation: Donation,
    field_data: Mapping[str, object],
) -> dict[str, float]:
    """Return source-aware confidence scores for QA badge rendering.

    Donor-confidence badges are hidden when donor values come from a matched
    source donor record (house file or data file). Donation/payment badges
    remain visible because those values still come from OCR extraction.
    """
    raw_confidence = field_data.get("confidence", {})
    if not isinstance(raw_confidence, Mapping):
        return {}

    confidence_scores: dict[str, float] = {}
    for key, value in raw_confidence.items():
        if not isinstance(key, str) or not isinstance(value, (int, float)):
            continue
        if value <= 0:
            continue
        confidence_scores[key] = float(value)

    if not confidence_scores:
        return confidence_scores

    donor_match_status = str(field_data.get("donor_match_status") or "")
    has_source_matched_donor = donor_match_status == "matched" and (
        donation.system_donor_id is not None
        or donation.donor_id is not None
        or donation.data_file_donor_id is not None
    )
    if not has_source_matched_donor:
        return confidence_scores

    return {
        field_name: score
        for field_name, score in confidence_scores.items()
        if field_name not in _DONOR_SOURCE_CONFIDENCE_FIELDS
    }


def _apply_donation_edits_from_post(
    request: HttpRequest, donation: Donation
) -> tuple[bool, bool]:
    """Persist donation, donor, and package-code fields from POST (same as save).

    Returns:
        (donation_or_package_changed, donor_changed)
    """
    donation_updates = update_donation_from_post(donation, request.POST)  # pyright: ignore[reportArgumentType]
    donor_updates = update_donor_from_post(donation, request.POST)  # pyright: ignore[reportArgumentType]
    package_code_updates = update_package_code_from_post(  # pyright: ignore[reportArgumentType]
        donation, request.POST
    )
    if donation_updates or package_code_updates:
        update_fields = list(
            dict.fromkeys([*donation_updates, *package_code_updates, "updated_at"])
        )
        donation.save(update_fields=update_fields)
    return (
        bool(donation_updates or package_code_updates),
        bool(donor_updates),
    )


def _batch_approval_blocked_response(
    request: HttpRequest, batch_id: int, batch: DonationBatch
) -> HttpResponse | None:
    """If unresolved card QA remains, return redirect with message; else None."""
    from scans.models import RedactionSettings

    required_methods = RedactionSettings.get_settings().required_payment_methods()
    if required_methods:
        pending_redaction = (
            batch.donations.filter(
                qa_status__in=[Donation.QA_STATUS_PENDING, Donation.QA_STATUS_FLAGGED],
                scan_placeholder__isnull=False,
                payment_method__in=required_methods,
            )
            .exclude(
                scan_placeholder__redaction_status__in=(
                    ScanPlaceholder.REDACTION_CVV_PENDING,
                    ScanPlaceholder.REDACTION_DEFERRED,
                    ScanPlaceholder.REDACTION_COMPLETED,
                )
            )
            .order_by("created_at", "id")
            .first()
        )
        if pending_redaction is not None:
            messages.error(
                request,
                "Batch approval is blocked because at least one scanned donation "
                "still needs QA redaction.",
            )
            return redirect(
                "custom_admin:qa_single_donation_review",
                batch_id=batch_id,
                donation_id=pending_redaction.id,
            )

    unresolved = batch.donations.filter(
        qa_status__in=[Donation.QA_STATUS_PENDING, Donation.QA_STATUS_FLAGGED],
        payment_method__in=_CARD_PAYMENT_METHODS,
        payment_status__in=["pending", "failed"],
    ).order_by("created_at", "id")
    if not unresolved.exists():
        return None
    first_card = unresolved.first()
    messages.error(
        request,
        "Batch approval is blocked because at least one card donation still "
        "requires secure payment details and individual QA approval.",
    )
    if first_card is not None:
        return redirect(
            "custom_admin:qa_single_donation_review",
            batch_id=batch_id,
            donation_id=first_card.id,
        )
    return redirect("custom_admin:qa_batch_review", batch_id=batch_id)


def _next_attention_review_url(
    batch_id: int,
    donation_ids: list[str],
    donations_qs: QuerySet[Donation],
    current_donation_id: str,
) -> str | None:
    """URL of the next pending/flagged donation after *current* in list order (wrap)."""
    rows = list(donations_qs.values("id", "qa_status"))
    status_map = {str(r["id"]): r["qa_status"] for r in rows}
    ids_str = [str(d) for d in donation_ids]
    cur = str(current_donation_id)
    try:
        cur_idx = ids_str.index(cur)
    except ValueError:
        return None
    for j in range(cur_idx + 1, len(ids_str)):
        did = ids_str[j]
        if status_map.get(did) in _ATTENTION_QA_STATUSES:
            return reverse(
                "custom_admin:qa_single_donation_review",
                args=[batch_id, did],
            )
    for j in range(0, cur_idx):
        did = ids_str[j]
        if status_map.get(did) in _ATTENTION_QA_STATUSES:
            return reverse(
                "custom_admin:qa_single_donation_review",
                args=[batch_id, did],
            )
    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _compute_qa_dashboard_stats() -> dict[str, int]:
    """Compute the four QA dashboard counters from the database.

    Pulled out as a module-level function so tests can observe call counts
    when verifying that the lock-on-rebuild path collapses concurrent loads
    to a single recompute.
    """
    return {
        "pending_qa": DonationBatch.objects.filter(
            status=DonationBatch.STATUS_PENDING_QA
        ).count(),
        "in_review": DonationBatch.objects.filter(
            status=DonationBatch.STATUS_IN_REVIEW
        ).count(),
        "approved_today": DonationBatch.objects.filter(
            status=DonationBatch.STATUS_APPROVED,
            reviewed_at__date=timezone.now().date(),
        ).count(),
        "rejected": DonationBatch.objects.filter(
            status=DonationBatch.STATUS_REJECTED
        ).count(),
    }


def _current_qa_dashboard_stats_version() -> int:
    """Return the current QA stats version counter, seeding it if absent.

    The counter lives in Redis (or LocMem in tests). ``cache.incr`` is atomic
    on the django-redis backend, but raises ``ValueError`` when the key is
    missing on the stdlib backends, so we seed with ``cache.add`` first —
    ``add`` is itself an atomic SETNX, so the seed is safe under concurrency.
    """
    version = cache.get(_QA_DASHBOARD_STATS_VERSION_KEY)
    if isinstance(version, int):
        return version
    cache.add(_QA_DASHBOARD_STATS_VERSION_KEY, 0)
    seeded = cache.get(_QA_DASHBOARD_STATS_VERSION_KEY, 0)
    return int(seeded) if isinstance(seeded, (int, str)) else 0


def _bump_dashboard_stats_version() -> None:
    """Atomically bump the QA dashboard stats version counter.

    Called from every batch-state-change site instead of ``cache.delete``.
    Old ``qa_dashboard_stats:v{n}`` entries naturally expire under their TTL
    so no manual cleanup is needed. ``cache.incr`` is atomic on Redis; the
    ValueError fallback covers the cold-key case on the stdlib LocMem backend
    used in tests.
    """
    try:
        cache.incr(_QA_DASHBOARD_STATS_VERSION_KEY)
    except ValueError:
        # Key missing — seed at 1 so the next dashboard read sees a bumped
        # version. ``cache.add`` is SETNX-atomic, so a parallel writer that
        # seeded just before us simply needs its value bumped instead.
        if not cache.add(_QA_DASHBOARD_STATS_VERSION_KEY, 1):
            with contextlib.suppress(ValueError):
                cache.incr(_QA_DASHBOARD_STATS_VERSION_KEY)


def _load_qa_dashboard_stats() -> dict[str, int]:
    """Return cached QA stats with a lock-on-rebuild stampede guard.

    Reads the current version, checks for a cached payload at that version,
    and recomputes only when missing. The recomputing process holds a short
    Redis lock (via ``cache.add`` SETNX) so concurrent readers hitting the
    same cold version don't all run the four COUNT queries — losers fall
    back to the previous version's payload (or fresh stats as a last resort
    when the cache is fully cold).
    """
    version = _current_qa_dashboard_stats_version()
    stats_key = f"qa_dashboard_stats:v{version}"

    cached = cache.get(stats_key)
    if isinstance(cached, dict):
        return cast(dict[str, int], cached)

    lock_key = f"{stats_key}:lock"
    if cache.add(lock_key, "1", _QA_DASHBOARD_STATS_LOCK_TTL_SECONDS):
        try:
            stats = _compute_qa_dashboard_stats()
            cache.set(stats_key, stats, _QA_DASHBOARD_STATS_TTL_SECONDS)
            return stats
        finally:
            cache.delete(lock_key)

    # Lock loser path — return the previous version's payload if present so
    # the dashboard renders without blocking. Falling back to a fresh compute
    # only happens on truly cold caches (first-ever load on a new replica).
    if version > 0:
        previous = cache.get(f"qa_dashboard_stats:v{version - 1}")
        if isinstance(previous, dict):
            return cast(dict[str, int], previous)
    return _compute_qa_dashboard_stats()


def _capture_moto_auths_for_batch(batch: DonationBatch) -> list[Donation]:
    """Capture every requires_capture MOTO auth in *batch*.

    Iterates the batch's donations whose ``payment_status`` is
    ``requires_capture`` and calls
    :meth:`payments.services.StripePaymentService.capture_payment_intent`
    for each. Idempotent — Stripe replays an identical capture on retry
    via the per-payment idempotency key.

    On capture failure the donation is reverted from ``qa_status=approved``
    back to ``qa_status=flagged`` and the batch-cascade auto-approval is
    undone for that single donation. This keeps it in the QA queue and
    ensures it cannot reach :func:`build_letter_generation_queryset`,
    which would otherwise generate a thank-you letter for a payment that
    never settled (``payment_status=failed`` is not on the letter
    queryset's exclude list).

    Returns:
        The list of donations whose capture failed, so callers can surface
        a count to the operator.
    """
    from payments.services import StripePaymentService

    # Deterministic ordering so a partial-failure run produces stable
    # results across DB backends and replays — important for the failure
    # reporting and for tests that mock individual capture outcomes.
    qs = (
        batch.donations.filter(
            payment_status=Donation.PAYMENT_STATUS_REQUIRES_CAPTURE,
        )
        .order_by("created_at", "id")
        .prefetch_related("stripe_payments")
    )
    failures: list[Donation] = []
    for donation in qs:
        payment = (
            donation.stripe_payments.filter(status="requires_capture")
            .order_by("-created_at")
            .first()
        )
        if payment is None:
            logger.warning(
                "MOTO donation %s has payment_status=requires_capture but no "
                "matching StripePayment row — reverting to flagged",
                donation.id,
            )
            donation.qa_status = Donation.QA_STATUS_FLAGGED
            donation.save(update_fields=["qa_status", "updated_at"])
            failures.append(donation)
            continue
        result = StripePaymentService.capture_payment_intent(str(payment.id))
        if not result.get("success"):
            logger.error(
                "MOTO capture failed for donation %s: %s — reverting to flagged",
                donation.id,
                result.get("error"),
            )
            # Revert the cascade auto-approve for this specific donation so
            # it stays in the QA queue rather than landing in the letter
            # generator with payment_status=failed.
            donation.qa_status = Donation.QA_STATUS_FLAGGED
            donation.save(update_fields=["qa_status", "updated_at"])
            failures.append(donation)
    return failures


def _cancel_moto_auths_for_batch(batch: DonationBatch) -> None:
    """Cancel every requires_capture MOTO auth in *batch* (bulk reject path)."""
    from payments.services import StripePaymentService

    qs = batch.donations.filter(
        payment_status=Donation.PAYMENT_STATUS_REQUIRES_CAPTURE,
    ).prefetch_related("stripe_payments")
    for donation in qs:
        payment = (
            donation.stripe_payments.filter(status="requires_capture")
            .order_by("-created_at")
            .first()
        )
        if payment is None:
            continue
        result = StripePaymentService.cancel_payment_intent(
            str(payment.id), reason="batch_rejected"
        )
        if not result.get("success"):
            logger.error(
                "MOTO cancel failed for donation %s: %s",
                donation.id,
                result.get("error"),
            )


def _commit_batch_status(
    request: HttpRequest,
    batch: DonationBatch,
    summary: str,
    changes: dict[str, Any],
) -> bool:
    """Save batch fields, bump stats cache version, cascade donation statuses, and audit log.

    Wrapped in :func:`transaction.atomic` so any failure (e.g. cascade update,
    audit log) rolls back the batch save together. Downstream Celery dispatches
    (registered via ``transaction.on_commit`` in :mod:`core.signals`) only fire
    once this transaction commits successfully.

    Idempotency guard: when the in-memory ``batch.status`` matches the
    persisted status (re-clicking the same terminal action, or selecting the
    dropdown's current value), this is a no-op — no save, no cascade, no
    signal fire, no audit log entry. This protects every approval path
    (``qa_approve_batch``, ``qa_reject_batch``, ``qa_batch_resubmit``,
    ``qa_update_batch_status``) at the source.

    Returns:
        ``True`` if the commit ran (status changed), ``False`` if the call
        was a no-op because the status was already at the target value.
    """
    persisted_status = (
        DonationBatch.objects.filter(pk=batch.pk)
        .values_list("status", flat=True)
        .first()
    )
    if persisted_status == batch.status:
        return False

    with transaction.atomic():
        batch.save(
            update_fields=["status", "reviewed_by", "reviewed_at", "review_notes"]
        )
        # Critical: sync donation.qa_status so they appear in Daily Banking.
        # Preserve per-donation rejections by only touching pending/flagged.
        # Issue 23: donations with non-empty ``low_confidence_fields`` require
        # explicit per-donation reviewer action — they MUST NOT ride the
        # cascade auto-approve, otherwise a low-confidence OCR amount would
        # silently flow through to thank-you letters and payment processing.
        # ``low_confidence_fields=[]`` matches both the JSON empty-list value
        # and the model default; SQLite + Postgres both support this filter.
        if batch.status == DonationBatch.STATUS_APPROVED:
            non_final = batch.donations.filter(
                qa_status__in=[Donation.QA_STATUS_PENDING, Donation.QA_STATUS_FLAGGED],
                low_confidence_fields=[],
            )
            # Snapshot IDs *before* the bulk update so we know which
            # donations actually transitioned into APPROVED — we need
            # them for the HGV alert below, and after the update they'd
            # all show ``qa_status=APPROVED`` and be indistinguishable
            # from donations that the reviewer approved earlier.
            cascaded_donation_ids = list(non_final.values_list("id", flat=True))
            if cascaded_donation_ids:
                non_final.update(
                    qa_status=Donation.QA_STATUS_APPROVED, updated_at=timezone.now()
                )
                # Defer HGV email until *after* commit so a rolled-back
                # batch save (e.g. MOTO capture failure path below)
                # doesn't fire an alert for a transition that never
                # actually persisted.
                transaction.on_commit(
                    lambda ids=cascaded_donation_ids: (
                        _fire_hgv_notifications_for_cascade(ids)
                    )
                )
            # MOTO (phone-intake) auths were captured at intake but settlement
            # was deferred to this approval. Capture each auth now so the
            # funds settle in lockstep with the batch approval. Donations
            # whose capture fails are reverted to ``qa_status=flagged`` by
            # the helper so they stay in the QA queue and don't reach the
            # letter generator with a failed payment.
            moto_capture_failures = _capture_moto_auths_for_batch(batch)
            if moto_capture_failures:
                messages.warning(
                    request,
                    f"{len(moto_capture_failures)} card capture(s) failed and "
                    "were returned to the QA queue. Investigate before "
                    "re-approving — donor cards may need a fresh auth.",
                )
            # Non-card donations have no asynchronous charge step that can
            # trigger redaction post-hoc — fire it now that the batch is
            # approved. Card donations defer redaction until their Stripe
            # charge succeeds (handled by ``BatchPaymentService``).
            _enqueue_deferred_redactions_for_batch(batch, only_non_card=True)
        elif batch.status == DonationBatch.STATUS_REJECTED:
            # Bulk reject: release MOTO auths so we don't leave the donor's
            # card with a 7-day pending hold for a charge that will never
            # settle.
            _cancel_moto_auths_for_batch(batch)
        log_request_action(
            request,
            action="UPDATE",
            model_name="DonationBatch",
            object_id=str(batch.id),
            object_repr=batch.batch_name,
            summary=summary,
            changes=changes,
        )
    # The version bump runs outside the atomic block on purpose: if the
    # block raises, no state changed in the DB so the previously cached
    # stats are still valid. Bumping (instead of deleting) is what defeats
    # the cache stampede — readers always see the new version key, and
    # the lock-on-rebuild path in ``_load_qa_dashboard_stats`` ensures only
    # one process recomputes while the others fall back to the prior
    # version's still-fresh payload.
    _bump_dashboard_stats_version()
    return True


# ---------------------------------------------------------------------------
# Approve / Reject entire batch
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
@has_permission_or_is_staff("change_donationbatch")
@require_http_methods(["POST"])
def qa_approve_batch(request: HttpRequest, batch_id: int) -> HttpResponse:
    """Approve the entire batch, auto-approving any remaining pending/flagged donations.

    Idempotent: a second submission for an already-approved batch is a no-op.
    This protects against double-clicks on the Approve button which would
    otherwise re-fire the post_save signal and duplicate downstream side
    effects (status emails, gift aid CSV regeneration, letter generation).
    """
    batch = get_object_or_404(DonationBatch, id=batch_id)

    if batch.status == DonationBatch.STATUS_APPROVED:
        messages.info(
            request,
            f'Batch "{batch.batch_name}" is already approved.',
        )
        return redirect("custom_admin:qa_dashboard")

    try:
        blocked = _batch_approval_blocked_response(request, batch_id, batch)
        if blocked is not None:
            return blocked

        total_donations = batch.donations.count()
        approved = batch.donations.filter(qa_status=Donation.QA_STATUS_APPROVED).count()
        rejected = batch.donations.filter(qa_status=Donation.QA_STATUS_REJECTED).count()
        pending = total_donations - approved - rejected
        # Issue 23: pending donations split into "auto-approvable" vs "held for
        # mandatory review" based on ``low_confidence_fields``. Held donations
        # remain flagged after the cascade and require explicit reviewer action.
        held = (
            batch.donations.filter(
                qa_status__in=[Donation.QA_STATUS_PENDING, Donation.QA_STATUS_FLAGGED],
            )
            .exclude(low_confidence_fields=[])
            .count()
        )
        auto_approved = max(pending - held, 0)

        old_status = batch.status
        batch.status = DonationBatch.STATUS_APPROVED  # type: ignore[assignment]
        batch.reviewed_by = request.user  # type: ignore[assignment]
        batch.reviewed_at = timezone.now()
        batch_notes = request.POST.get("batch_notes", "").strip()

        # Issue 2: auto-approve remaining pending/flagged rather than blocking the batch.
        note_parts: list[str] = []
        if auto_approved > 0:
            note_parts.append(
                f"{auto_approved} pending donation(s) auto-approved on batch approval."
            )
        if held > 0:
            note_parts.append(
                f"{held} donation(s) held for mandatory review (low OCR confidence)."
            )
        auto_note = " ".join(note_parts)
        if auto_note:
            batch.review_notes = (
                f"{batch_notes} {auto_note}".strip() if batch_notes else auto_note
            )
        elif batch_notes:
            batch.review_notes = batch_notes

        _commit_batch_status(
            request,
            batch,
            summary=f"QA approved batch {batch.batch_name} ({approved} approved, {rejected} rejected)",
            changes={
                "old_status": old_status,
                "new_status": DonationBatch.STATUS_APPROVED,
                "approved_donations": approved,
                "rejected_donations": rejected,
                "total_donations": total_donations,
            },
        )

        held_suffix = (
            f" {held} held for mandatory review (low OCR confidence)."
            if held > 0
            else ""
        )
        if rejected > 0:
            messages.success(
                request,
                f'Batch "{batch.batch_name}" approved. {approved} approved, {rejected} rejected'
                + (f", {auto_approved} auto-approved." if auto_approved > 0 else ".")
                + held_suffix,
            )
        else:
            messages.success(
                request,
                f'Batch "{batch.batch_name}" approved. {total_donations} donations approved'
                + (f" ({auto_approved} auto-approved)." if auto_approved > 0 else "."),
            )
            if held_suffix:
                messages.warning(request, held_suffix.strip())
        if batch.created_by:
            messages.info(
                request,
                f"Notification sent to {batch.created_by.get_full_name() or batch.created_by.username}",
            )
        return redirect("custom_admin:qa_dashboard")
    finally:
        _release_reviewer_lock(batch.id, cast(User, request.user))


@is_authenticated_and_is_staff
@has_permission_or_is_staff("change_donationbatch")
@require_http_methods(["POST"])
def qa_reject_batch(request: HttpRequest, batch_id: int) -> HttpResponse:
    """Reject the entire batch with mandatory notes."""
    batch = get_object_or_404(DonationBatch, id=batch_id)
    batch_notes = request.POST.get("batch_notes", "").strip()

    if not batch_notes:
        messages.error(request, "Rejection notes are required when rejecting a batch.")
        last = batch.donations.order_by("created_at", "id").last()
        if last:
            return redirect(
                "custom_admin:qa_single_donation_review",
                batch_id=batch_id,
                donation_id=last.id,
            )
        return redirect("custom_admin:qa_batch_review", batch_id=batch_id)

    try:
        old_status = batch.status
        batch.status = DonationBatch.STATUS_REJECTED  # type: ignore[assignment]
        batch.reviewed_by = request.user  # type: ignore[assignment]
        batch.reviewed_at = timezone.now()
        batch.review_notes = batch_notes

        _commit_batch_status(
            request,
            batch,
            summary=f"QA rejected batch {batch.batch_name}",
            changes={
                "old_status": old_status,
                "new_status": DonationBatch.STATUS_REJECTED,
                "rejection_notes": batch_notes,
            },
        )

        _enqueue_deferred_redactions_for_batch(batch)

        messages.warning(
            request,
            f'Batch "{batch.batch_name}" rejected. The batch creator has been notified.',
        )
        candidate = getattr(batch, "created_by", None)
        if candidate:
            Notification.objects.create(
                user=candidate,
                title=f'Batch "{batch.batch_name}" rejected',
                message=(
                    f'QA rejected batch "{batch.batch_name}". Reviewer notes: {batch_notes}'
                ),
                notification_type=Notification.TYPE_WARNING,
                related_object_type="DonationBatch",
                related_object_id=str(batch.id),
                link=reverse(
                    "custom_admin:qa_batch_review", kwargs={"batch_id": batch.id}
                ),
            )
            messages.info(
                request,
                f"Notification sent to {candidate.get_full_name() or candidate.username}",
            )
        return redirect("custom_admin:qa_dashboard")
    finally:
        _release_reviewer_lock(batch.id, cast(User, request.user))


@is_authenticated_and_is_staff
@has_permission_or_is_staff("change_donationbatch")
@require_http_methods(["POST"])
def qa_batch_resubmit(request: HttpRequest, batch_id: int) -> HttpResponse:
    """Re-submit a rejected batch back into the QA queue."""
    batch = get_object_or_404(DonationBatch, id=batch_id)

    if batch.status != DonationBatch.STATUS_REJECTED:
        messages.error(request, "Only rejected batches can be re-submitted to QA.")
        return redirect("custom_admin:qa_batch_review", batch_id=batch_id)

    try:
        old_status = batch.status
        batch.status = DonationBatch.STATUS_PENDING_QA  # type: ignore[assignment]
        batch.reviewed_by = None  # type: ignore[assignment]
        batch.reviewed_at = None
        batch.review_notes = ""

        _commit_batch_status(
            request,
            batch,
            summary=f"Batch {batch.batch_name} re-submitted to QA after rejection",
            changes={
                "old_status": old_status,
                "new_status": DonationBatch.STATUS_PENDING_QA,
            },
        )
        messages.success(
            request, f'Batch "{batch.batch_name}" has been re-submitted to QA.'
        )
        return redirect("custom_admin:qa_dashboard")
    finally:
        _release_reviewer_lock(batch.id, cast(User, request.user))


_FILTER_KEYS = {"status", "client", "campaign", "created_by", "search"}


def _build_custom_field_items(donation: Donation) -> list[tuple[str, object]]:
    """Return campaign-defined custom fields for QA display.

    Args:
        donation: Donation being reviewed.

    Returns:
        Ordered ``(label, value)`` pairs for configured campaign custom fields.
    """
    field_data = donation.field_data or {}
    if not field_data:
        return []

    items: list[tuple[str, object]] = []
    campaign_fields = donation.campaign.fields.filter(is_default_field=False).only(
        "id", "label"
    )
    for field in campaign_fields:
        field_key = str(field.id)
        if field_key not in field_data:
            continue
        items.append((field.label, field_data[field_key]))
    return items


def _apply_batch_filters(
    qs: object,
    status: str,
    client: str,
    campaign: str,
    created_by: str,
    search: str,
) -> object:
    """Apply optional filters to the batches queryset."""
    if status and status != "all":
        qs = qs.filter(status=status)
    if client:
        qs = qs.filter(campaign__client_id=client)
    if campaign:
        qs = qs.filter(campaign_id=campaign)
    if created_by:
        qs = qs.filter(created_by_id=created_by)
    if search:
        qs = qs.filter(
            Q(batch_name__icontains=search)
            | Q(campaign__name__icontains=search)
            | Q(campaign__client__name__icontains=search)
            | Q(created_by__username__icontains=search)
            | Q(created_by__first_name__icontains=search)
            | Q(created_by__last_name__icontains=search)
        )
    return qs


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@qa_access_required
def qa_dashboard(request: HttpRequest) -> HttpResponse:
    """QA Dashboard showing all batches requiring review."""
    session_key = "qa_filters"

    if request.GET.get("clear"):
        request.session.pop(session_key, None)
        return redirect("custom_admin:qa_dashboard")

    redir = restore_session_filters(
        request,
        session_key,
        "custom_admin:qa_dashboard",
        filter_keys=tuple(_FILTER_KEYS),
    )
    if redir:
        return redir

    # Extract filter params
    status_filter = request.GET.get("status", "") or "pending_qa"
    client_filter = request.GET.get("client", "")
    campaign_filter = request.GET.get("campaign", "")
    created_by_filter = request.GET.get("created_by", "")
    search_query = request.GET.get("search", "").strip()

    has_params = any(k in request.GET for k in _FILTER_KEYS)
    if has_params:
        request.session[session_key] = {
            k: request.GET[k] for k in _FILTER_KEYS if request.GET.get(k)
        }

    # Build queryset
    batches_qs = (
        DonationBatch.objects.select_related(
            "campaign", "campaign__client", "created_by", "reviewed_by"
        )
        .only(
            "id",
            "batch_name",
            "status",
            "created_at",
            "reviewed_at",
            "campaign__name",
            "campaign__client__name",
            "created_by__username",
            "created_by__first_name",
            "created_by__last_name",
            "reviewed_by__username",
            "reviewed_by__first_name",
            "reviewed_by__last_name",
            "default_payment_method",
        )
        .annotate(
            donation_count=Count("donations"),
            total_amount_sum=Sum("donations__amount"),
        )
    )

    batches_qs = _apply_batch_filters(
        batches_qs,
        status_filter,
        client_filter,
        campaign_filter,
        created_by_filter,
        search_query,
    )
    batches_qs = batches_qs.order_by("-created_at")
    batches = paginate_queryset(batches_qs, request, per_page=20)

    stats = _load_qa_dashboard_stats()

    clients = cache.get_or_set(
        "qa_filter_clients",
        lambda: list(
            Client.objects.filter(is_active=True).only("id", "name").order_by("name")
        ),
        300,
    )
    campaigns = cache.get_or_set(
        "qa_filter_campaigns",
        lambda: list(
            Campaign.objects.filter(status="active").only("id", "name").order_by("name")
        ),
        300,
    )
    creators = cache.get_or_set(
        "qa_filter_creators",
        lambda: list(
            User.objects.filter(created_batches__isnull=False)
            .distinct()
            .only("id", "username", "first_name", "last_name")
            .order_by("username")
        ),
        300,
    )

    context = {
        "active": "qa_review",
        "batches": batches,
        "stats": stats,
        "clients": clients,
        "campaigns": campaigns,
        "creators": creators,
        "status_filter": status_filter,
        "client_filter": client_filter,
        "campaign_filter": campaign_filter,
        "created_by_filter": created_by_filter,
        "search_query": search_query,
        "page_title": "QA Review Dashboard",
    }
    return render(request, "admin/qa/dashboard.html", context)


# ---------------------------------------------------------------------------
# Single-donation review
# ---------------------------------------------------------------------------


@qa_access_required
def qa_batch_review(request: HttpRequest, batch_id: int) -> HttpResponse:
    """Entry point for batch review; routes to one-on-one view."""
    return _render_single_donation_review(request, batch_id)


@qa_access_required
def qa_single_donation_review(
    request: HttpRequest, batch_id: int, donation_id: str
) -> HttpResponse:
    """Direct navigation to a specific donation within a batch."""
    return _render_single_donation_review(request, batch_id, donation_id)


def _build_review_nav(
    batch_id: int, donation_ids: list[str], position: int
) -> dict[str, str | int | None]:
    """Build prev/next/last navigation URLs and indices."""
    total = len(donation_ids)
    prev_id = donation_ids[position - 1] if position > 0 else None
    next_id = donation_ids[position + 1] if position + 1 < total else None

    def _url(did: str | None) -> str | None:
        return (
            reverse("custom_admin:qa_single_donation_review", args=[batch_id, did])
            if did
            else None
        )

    return {
        "prev_id": prev_id,
        "next_id": next_id,
        "prev_url": _url(prev_id),
        "next_url": _url(next_id),
        "last_url": reverse(
            "custom_admin:qa_single_donation_review",
            args=[batch_id, donation_ids[-1]],
        ),
        "position": position + 1,
        "total": total,
    }


def _build_donation_stats(donations_qs: QuerySet[Donation]) -> dict[str, int]:
    """Return donation-level QA stats from the queryset."""
    raw = donations_qs.aggregate(
        approved_count=Count("id", filter=Q(qa_status=Donation.QA_STATUS_APPROVED)),
        rejected_count=Count("id", filter=Q(qa_status=Donation.QA_STATUS_REJECTED)),
        pending_count=Count("id", filter=Q(qa_status=Donation.QA_STATUS_PENDING)),
        flagged_count=Count("id", filter=Q(qa_status=Donation.QA_STATUS_FLAGGED)),
    )
    return {k: raw.get(k, 0) for k in raw}


def _build_rejected_records(
    donations_qs: QuerySet[Donation],
    batch_id: int,
) -> list[dict[str, object]]:
    """Return rejected donation rows for batch-level rejection summary UI."""
    records: list[dict[str, object]] = []
    rejected_qs = donations_qs.filter(qa_status=Donation.QA_STATUS_REJECTED).order_by(
        "-updated_at", "-created_at", "id"
    )
    for rejected in rejected_qs:
        if rejected.system_donor_id and rejected.system_donor is not None:
            supporter_name = (
                f"{rejected.system_donor.first_name} "
                f"{rejected.system_donor.last_name}".strip()
                or "Unknown"
            )
        elif rejected.donor_id and rejected.donor is not None:
            supporter_name = (
                f"{rejected.donor.first_name} {rejected.donor.last_name}".strip()
                or "Unknown"
            )
        elif rejected.data_file_donor_id and rejected.data_file_donor is not None:
            supporter_name = (
                f"{rejected.data_file_donor.first_name} "
                f"{rejected.data_file_donor.last_name}".strip()
                or "Unknown"
            )
        else:
            supporter_name = "Unknown"
        supporter_urn = get_donation_display_urn(rejected)

        reason = (rejected.qa_notes or "").strip() or "No rejection reason provided."
        records.append(
            {
                "id": str(rejected.id),
                "detail_url": reverse(
                    "custom_admin:qa_single_donation_review",
                    args=[batch_id, rejected.id],
                ),
                "supporter_name": supporter_name,
                "supporter_urn": supporter_urn,
                "amount": rejected.amount,
                "donation_date": rejected.donation_date,
                "updated_at": rejected.updated_at,
                "reason": reason,
            }
        )
    return records


def _build_review_context(
    batch: DonationBatch,
    donation: Donation,
    donations_qs: QuerySet[Donation],
    donation_ids: list[str],
    nav: dict[str, str | int | None],
    position: int,
    *,
    user: User | None = None,
) -> dict[str, object]:
    """Assemble template context for the donation review page."""
    from payments.services import StripePaymentError, StripePaymentService
    from scans.scan_redaction import manual_redaction_required_for

    total = int(nav["total"] or 0)
    preload_images = _preload_form_images(
        donations_qs, donation_ids, position, user=user
    )
    dstats = _build_donation_stats(donations_qs)

    totals = donations_qs.aggregate(
        total_amount=Sum("amount"),
        gift_aid_count=Count("id", filter=Q(gift_aid=True)),
    )
    pending = dstats.get("pending_count", 0) + dstats.get("flagged_count", 0)
    attention_count = pending
    next_attention_url = _next_attention_review_url(
        batch.id, donation_ids, donations_qs, str(donation.id)
    )
    field_items = _build_custom_field_items(donation)
    rejected_records = _build_rejected_records(donations_qs, batch.id)

    # Extract per-field confidence scores for OCR badge display
    field_data = donation.field_data or {}
    confidence_scores = _confidence_scores_for_display(donation, field_data)
    is_ocr_extracted = field_data.get("ocr_extracted", False)
    record_type = field_data.get(
        "campaign_temperature", field_data.get("record_type", "")
    )
    identifier_source = field_data.get("identifier_source", "")
    donor_match_status = field_data.get("donor_match_status", "")
    exception_reason = field_data.get("exception_reason", "")

    # OCR extracted data from scan placeholder
    placeholder = getattr(donation, "scan_placeholder", None)
    pending_redaction = _pending_redaction_placeholder(donation)
    ocr_data: dict[str, object] = (
        (placeholder.extracted_data or {}) if placeholder else {}
    )
    ocr_meta: dict[str, object] = (placeholder.ocr_data or {}) if placeholder else {}
    unmapped_entity_labels = ocr_meta.get("unmapped_entity_labels", [])
    if not isinstance(unmapped_entity_labels, list):
        unmapped_entity_labels = []
    requires_card_tokenization = (
        donation.payment_method in _CARD_PAYMENT_METHODS
        and donation.payment_status in {"pending", "failed"}
    )
    stripe_publishable_key = ""
    stripe_unavailable_reason = ""
    if requires_card_tokenization:
        try:
            stripe_publishable_key = StripePaymentService._get_publishable_key(
                donation.campaign.client
            )
        except StripePaymentError as exc:
            stripe_unavailable_reason = str(exc)
            logger.warning(
                "Stripe publishable key unavailable for donation %s (client %s) — card capture disabled in QA: %s",
                donation.id,
                getattr(donation.campaign.client, "id", None),
                exc,
            )
            stripe_publishable_key = ""
        except Exception:
            stripe_unavailable_reason = "Unexpected server error — see logs."
            logger.exception(
                "Unexpected error resolving Stripe publishable key for donation %s (client %s) — card capture disabled in QA",
                donation.id,
                getattr(donation.campaign.client, "id", None),
            )
            stripe_publishable_key = ""

    # Issue 23: structured low-confidence records for the QA UI banner.
    # ``low_confidence_fields`` may contain primitive strings on legacy rows or
    # ``{field, confidence}`` dicts on new rows. Normalise to dicts so the
    # template can render either shape uniformly.
    raw_low = donation.low_confidence_fields or []
    low_confidence_records: list[dict[str, object]] = []
    if isinstance(raw_low, list):
        for entry in raw_low:
            if isinstance(entry, dict):
                low_confidence_records.append(
                    {
                        "field": str(entry.get("field", "")),
                        "confidence": entry.get("confidence"),
                    }
                )
            elif isinstance(entry, str):
                low_confidence_records.append({"field": entry, "confidence": None})

    awaiting_authentication = (
        donation.payment_status == Donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION
    )
    send_authentication_link_url = (
        reverse(
            "custom_admin:qa_send_authentication_link",
            args=[batch.id, donation.id],
        )
        if awaiting_authentication
        else ""
    )
    latest_payment_authentication_link_sent_at = None
    if awaiting_authentication:
        from payments.models import StripePayment

        latest_payment = (
            StripePayment.objects.filter(donation=donation)
            .order_by("-created_at")
            .only("authentication_link_sent_at")
            .first()
        )
        if latest_payment is not None:
            latest_payment_authentication_link_sent_at = (
                latest_payment.authentication_link_sent_at
            )

    return {
        "active": "qa_review",
        "batch": batch,
        "donation": donation,
        "low_confidence_records": low_confidence_records,
        "has_low_confidence_hold": bool(low_confidence_records),
        **nav,
        "total_donations": total,
        "preload_images": preload_images,
        "scanned_form_url": DonationScanService.get_scanned_form_url(
            donation,
            require_existing=True,
            user=user,
            allow_pending_redaction=True,
        ),
        "scanned_form_page_urls_json": json.dumps(
            DonationScanService.get_scanned_form_page_urls(
                donation,
                require_existing=True,
                user=user,
                allow_pending_redaction=True,
            )
        ),
        "predicted_scanned_form_url": DonationScanService.get_scanned_form_url(
            donation,
            require_existing=False,
            user=user,
            allow_pending_redaction=True,
        ),
        "has_scanned_form": DonationScanService.has_scanned_form(donation),
        "scan_placeholder_id": str(placeholder.id) if placeholder else "",
        "scan_redaction_required": bool(
            placeholder and manual_redaction_required_for(donation.payment_method or "")
        ),
        "scan_redaction_pending": pending_redaction is not None,
        "scan_redaction_status": (
            placeholder.redaction_status
            if placeholder
            else ScanPlaceholder.REDACTION_COMPLETED
        ),
        "scan_redaction_notes": placeholder.redaction_notes if placeholder else "",
        "scan_redaction_save_url": (
            reverse(
                "custom_admin:qa_save_scan_redaction",
                args=[batch.id, donation.id],
            )
            if placeholder
            else ""
        ),
        # OCR extracted fields for QA display
        "ocr_extracted_data": ocr_data,
        "unmapped_entity_labels": unmapped_entity_labels,
        "currency_choices": Donation.CURRENCY_CHOICES,
        "payment_methods": Donation.PAYMENT_METHOD_CHOICES,
        "frequency_choices": Donation.FREQUENCY_CHOICES,
        "totals": {
            "count": total,
            "total_amount": totals.get("total_amount") or 0,
            "gift_aid_count": totals.get("gift_aid_count") or 0,
        },
        "active_donor": get_active_donor(donation),
        "package_code": get_package_code_value(donation),
        "field_data_items": field_items,
        "confidence_scores": confidence_scores,
        "is_ocr_extracted": is_ocr_extracted,
        "record_type": record_type,
        "identifier_source": identifier_source,
        "donor_match_status": donor_match_status,
        "exception_reason": exception_reason,
        "qa_notes_prefill": (
            ""
            if _is_system_generated_qa_note(donation.qa_notes or "")
            else donation.qa_notes
        ),
        "qa_reject_reason_choices": Donation.QA_REJECT_REASON_CHOICES,
        "qa_reject_reason_current": donation.qa_reject_reason,
        "qa_reject_reason_other_value": Donation.QA_REJECT_REASON_OTHER,
        "donor_contact_status_choices": Donor.CONTACT_STATUS_CHOICES,
        "donor_contact_status_current": _current_donor_contact_status(donation),
        "donor_contact_status_reason_current": _current_donor_contact_status_reason(
            donation
        ),
        "page_title": f"QA Review: {batch.batch_name}",
        "is_last_donation": nav["position"] == total,
        "all_donations_approved": dstats.get("approved_count", 0) == total
        and total > 0,
        "all_donations_reviewed": pending == 0 and total > 0,
        "donation_stats": {**dstats, "total": total},
        "rejected_records": rejected_records,
        "requires_card_tokenization": requires_card_tokenization,
        "stripe_publishable_key": stripe_publishable_key,
        "stripe_gateway_ready": bool(stripe_publishable_key),
        "stripe_unavailable_reason": stripe_unavailable_reason,
        "attention_count": attention_count,
        "next_attention_url": next_attention_url,
        "awaiting_authentication": awaiting_authentication,
        "send_authentication_link_url": send_authentication_link_url,
        "authentication_link_sent_at": (latest_payment_authentication_link_sent_at),
        # Default to today only when the donation has no stored date — for OCR'd
        # rows the extracted cheque-write date is rarely what charities want
        # recorded. Showing the persisted value on already-reviewed donations
        # avoids silently overwriting it on a benign re-save.
        "donation_date_initial": donation.donation_date or timezone.localdate(),
    }


def _render_single_donation_review(
    request: HttpRequest,
    batch_id: int,
    donation_id: str | None = None,
) -> HttpResponse:
    """Render the one-on-one QA review for a batch donation."""
    batch = get_object_or_404(
        DonationBatch.objects.select_related(
            "campaign", "campaign__client", "created_by", "reviewed_by"
        ).only(
            "id",
            "batch_name",
            "status",
            "created_at",
            "reviewed_at",
            "review_notes",
            "default_payment_method",
            "campaign__name",
            "campaign__client__name",
            "created_by__username",
            "created_by__first_name",
            "created_by__last_name",
            "reviewed_by__username",
            "reviewed_by__first_name",
            "reviewed_by__last_name",
        ),
        id=batch_id,
    )

    # The QA template + helpers traverse all of these per donation row;
    # without select_related, a 50-donation batch issues a query per FK
    # per row. Pinned by tests/unit/test_qa_review_perf.py.
    donations_qs = (
        Donation.objects.filter(batch=batch)
        .select_related(
            "donor",
            "data_file_donor",
            "system_donor",
            "scan_placeholder",
            "batch",
            "campaign",
            "campaign__client",
        )
        .order_by("created_at", "id")
    )

    donation_ids = list(donations_qs.values_list("id", flat=True))
    if not donation_ids:
        messages.info(request, "No donations available in this batch yet.")
        return redirect("custom_admin:qa_dashboard")

    target_id = donation_id or donation_ids[0]
    donation = get_object_or_404(donations_qs, id=target_id)
    position = donation_ids.index(donation.id)
    nav = _build_review_nav(batch.id, donation_ids, position)

    if request.method == "POST":
        return _handle_review_post(request, batch, donation, nav["next_id"])  # pyright: ignore[reportArgumentType]

    # GET path: claim (or refresh) the reviewer lock so two QA reviewers
    # cannot edit the same batch simultaneously. POST handlers verify the
    # claim still belongs to the caller before persisting any changes.
    user = cast(User, request.user)
    batch, conflicting_holder = _claim_reviewer_lock(batch.id, user)
    lock_context = _reviewer_lock_template_context(batch, conflicting_holder)
    if conflicting_holder is not None:
        messages.warning(request, str(lock_context["reviewer_lock_message"]))

    ctx = _build_review_context(
        batch,
        donation,
        donations_qs,
        donation_ids,
        nav,
        position,
        user=user if request.user.is_authenticated else None,
    )
    ctx.update(lock_context)
    return render(request, "admin/qa/donation_review.html", ctx)


def _redirect_to_donation(batch_id: int, donation_id: str) -> HttpResponse:
    """Redirect to a specific donation review."""
    return redirect(
        "custom_admin:qa_single_donation_review",
        batch_id=batch_id,
        donation_id=donation_id,
    )


def _handle_save_donation(
    request: HttpRequest, batch: DonationBatch, donation: Donation
) -> HttpResponse:
    """Save donation field edits during QA review."""
    _enforce_reviewer_lock_or_raise(batch, cast(User, request.user))

    missing_required = _missing_required_qa_fields(request.POST)
    if missing_required:
        messages.error(
            request,
            "Please complete required fields before saving: "
            + ", ".join(missing_required)
            + ".",
        )
        return _redirect_to_donation(batch.id, donation.id)

    d_changed, donor_changed = _apply_donation_edits_from_post(request, donation)
    if d_changed or donor_changed:
        messages.success(request, "Donation details updated for QA review.")
    else:
        messages.info(request, "No changes detected to save.")

    return _redirect_to_donation(batch.id, donation.id)


_QA_STATUS_MAP = {
    "approve": Donation.QA_STATUS_APPROVED,
    "reject": Donation.QA_STATUS_REJECTED,
    "flag": Donation.QA_STATUS_FLAGGED,
    "reset": Donation.QA_STATUS_PENDING,
}


def _fire_hgv_notification_for_approval(donation: Donation) -> None:
    """Send the HGV alert for a donation that has just transitioned to APPROVED.

    Logs but never re-raises — a Resend outage must not roll back the QA
    approval transaction. The threshold + recipient checks live inside
    ``send_hgv_notification`` itself so this stays a thin wrapper.
    """
    campaign = getattr(donation, "campaign", None)
    if campaign is None:
        return
    try:
        send_hgv_notification(donation, campaign)
    except Exception:
        logger.exception(
            "HGV notification failed for donation %s on QA approval", donation.pk
        )


def _fire_rejection_notification(
    donation: Donation, request: HttpRequest | None
) -> None:
    """Email the ops inbox about a freshly rejected donation.

    Mirrors :func:`_fire_hgv_notification_for_approval` — the helper itself
    is a no-op when ``OPERATIONS_REJECT_EMAIL`` is unset, but we still wrap
    the call in a try/except so a Resend / SMTP outage cannot roll back the
    QA rejection.
    """
    try:
        send_rejection_notification(donation, request=request)
    except Exception:
        logger.exception(
            "QA rejection notification failed for donation %s", donation.pk
        )


def _fire_hgv_notifications_for_cascade(donation_ids: list[int]) -> None:
    """Send HGV alerts for donations auto-approved as part of a batch cascade.

    Loaded with ``select_related`` so the per-donation
    ``send_hgv_notification`` call doesn't fan out to extra queries. Runs
    *after* the cascading transaction commits — see callers, which wrap
    this in ``transaction.on_commit``.
    """
    if not donation_ids:
        return
    donations = Donation.objects.select_related(
        "campaign", "campaign__client", "donor"
    ).filter(id__in=donation_ids)
    for donation in donations:
        _fire_hgv_notification_for_approval(donation)


def _handle_qa_action(
    request: HttpRequest,
    batch: DonationBatch,
    donation: Donation,
    next_id: str | None,
) -> HttpResponse:
    """Apply a QA status action (approve/reject/flag/reset)."""
    _enforce_reviewer_lock_or_raise(batch, cast(User, request.user))

    action = request.POST.get("action")
    if action not in _QA_STATUS_MAP:
        messages.error(request, "Invalid review action selected.")
        return _redirect_to_donation(batch.id, donation.id)

    missing_required = _missing_required_qa_fields(request.POST)
    if missing_required:
        messages.error(
            request,
            "Please complete required fields before review action: "
            + ", ".join(missing_required)
            + ".",
        )
        return _redirect_to_donation(batch.id, donation.id)

    notes = request.POST.get("qa_notes", "").strip()
    reject_reason = request.POST.get("qa_reject_reason", "").strip()
    valid_reject_reasons = {value for value, _ in Donation.QA_REJECT_REASON_CHOICES}
    if action == "reject":
        if reject_reason not in valid_reject_reasons:
            messages.error(
                request,
                "Select a valid reject reason before rejecting this donation.",
            )
            return _redirect_to_donation(batch.id, donation.id)
        if reject_reason == Donation.QA_REJECT_REASON_OTHER and not notes:
            messages.error(
                request,
                "Notes are required when the reject reason is 'Other'.",
            )
            return _redirect_to_donation(batch.id, donation.id)
    else:
        # Non-reject actions must not carry a stale reason.
        reject_reason = ""

    requires_immediate_payment = False
    sca_pending = False
    previous_qa_status: str | None = None
    with transaction.atomic():
        # Lock the Donation row for the lifetime of this txn so two QA
        # reviewers approving/rejecting the same donation cannot race and
        # silently overwrite each other's qa_status / qa_notes.
        donation = _lock_self(Donation.objects).get(pk=donation.pk)
        _apply_donation_edits_from_post(request, donation)
        donation.refresh_from_db()
        # Snapshot for the HGV-on-approval idempotency check below: only
        # fire the alert when the donation actually transitions into
        # APPROVED, not on every save of an already-approved donation.
        previous_qa_status = donation.qa_status

        if action == "approve":
            pending_redaction = _pending_redaction_placeholder(donation)
            if pending_redaction is not None:
                messages.error(
                    request,
                    "Complete QA redaction for this donor's scanned form before "
                    "approving the donation.",
                )
                transaction.set_rollback(True)
                return _redirect_to_donation(batch.id, donation.id)

        # MOTO phone-intake donations had their card authorised at intake;
        # QA approval triggers capture (settlement) and rejection cancels
        # the auth (no refund cycle needed, since funds were never settled).
        if (
            donation.payment_method in _CARD_PAYMENT_METHODS
            and donation.payment_status == Donation.PAYMENT_STATUS_REQUIRES_CAPTURE
        ):
            from payments.services import StripePaymentService

            payment = (
                donation.stripe_payments.filter(status="requires_capture")
                .order_by("-created_at")
                .first()
            )
            if payment is None:
                messages.error(
                    request,
                    "MOTO authorisation record missing — cannot proceed. "
                    "Investigate why payment_status=requires_capture has no "
                    "matching Stripe payment row.",
                )
                transaction.set_rollback(True)
                return _redirect_to_donation(batch.id, donation.id)

            if action == "approve":
                capture_result = StripePaymentService.capture_payment_intent(
                    str(payment.id)
                )
                if not capture_result.get("success"):
                    messages.error(
                        request,
                        "Card capture failed: "
                        + str(capture_result.get("error", "unknown")),
                    )
                    transaction.set_rollback(True)
                    return _redirect_to_donation(batch.id, donation.id)
                donation.refresh_from_db()
            elif action == "reject":
                cancel_result = StripePaymentService.cancel_payment_intent(
                    str(payment.id), reason="qa_rejected"
                )
                if not cancel_result.get("success"):
                    messages.error(
                        request,
                        "Card auth cancellation failed: "
                        + str(cancel_result.get("error", "unknown"))
                        + " — donation will not be marked rejected to avoid an "
                        "orphaned auth.",
                    )
                    transaction.set_rollback(True)
                    return _redirect_to_donation(batch.id, donation.id)
                donation.refresh_from_db()

        requires_immediate_payment = (
            action == "approve"
            and donation.payment_method in _CARD_PAYMENT_METHODS
            and donation.payment_status in {"pending", "failed"}
        )

        if requires_immediate_payment:
            payment_method_id = request.POST.get("stripe_payment_method_id", "").strip()
            if not payment_method_id:
                messages.error(
                    request,
                    "Secure card details are required before this donation can be approved.",
                )
                transaction.set_rollback(True)
                return _redirect_to_donation(batch.id, donation.id)

            from payments.batch_payment import BatchPaymentService

            result = BatchPaymentService.process_donation_payment(
                donation,
                cast(User, request.user),
                payment_method_id=payment_method_id,
                require_qa_approved=False,
            )
            if not result.get("success"):
                messages.error(
                    request,
                    str(
                        result.get(
                            "error", "Card payment failed. Donation not approved."
                        )
                    ),
                )
                transaction.set_rollback(True)
                return _redirect_to_donation(batch.id, donation.id)
            sca_pending = bool(result.get("awaiting_authentication"))
            donation.refresh_from_db()

        donation.qa_status = _QA_STATUS_MAP[action]
        if notes or action != "approve":
            donation.qa_notes = notes
        elif _is_system_generated_qa_note(donation.qa_notes or ""):
            # Remove legacy OCR-generated note text when QA approves without notes.
            donation.qa_notes = ""
        donation.qa_reject_reason = reject_reason
        donation.save(
            update_fields=[
                "qa_status",
                "qa_notes",
                "qa_reject_reason",
                "updated_at",
            ]
        )

    if action == "approve":
        # Observability: emit a Prometheus counter increment for every QA
        # approval so we can track reviewer throughput per client/method.
        client_obj = (
            getattr(donation.campaign, "client", None) if donation.campaign else None
        )
        record_qa_donation_approved(
            method=donation.payment_method,
            client=getattr(client_obj, "name", None),
        )
        # Non-card donations have no Stripe charge step, so redaction must
        # fire here at QA approval. Card donations defer until the charge
        # succeeds (handled by ``BatchPaymentService``).
        if donation.payment_method not in _CARD_PAYMENT_METHODS:
            _enqueue_deferred_redaction_if_pending(donation)
        # HGV email: only on first transition into APPROVED so re-saves
        # of an already-approved donation don't double-send.
        if previous_qa_status != Donation.QA_STATUS_APPROVED:
            _fire_hgv_notification_for_approval(donation)

    if action == "reject":
        # Operator has decided this donation will not be charged — apply the
        # deferred redaction now so the readable image doesn't sit around
        # indefinitely. Idempotent: a no-op if status isn't DEFERRED.
        _enqueue_deferred_redaction_if_pending(donation)
        # Notify the ops inbox so a human can decide whether to issue a
        # follow-up letter (Basecamp todo #14, Paul Nichols 2026-02-27).
        # Only fires from the explicit per-donation reject path — the
        # batch-cascade reject deliberately stays silent to avoid spamming
        # ops with one email per pending donation in a bulk reject.
        _fire_rejection_notification(donation, request)

    labels = dict(Donation.QA_STATUS_CHOICES)
    if requires_immediate_payment and sca_pending:
        messages.warning(
            request,
            "Donation approved. The donor's card needs 3DS / SCA "
            "authentication — send them an authentication link from the "
            "donation page to complete the charge.",
        )
    elif requires_immediate_payment:
        messages.success(
            request,
            "Card payment processed successfully and donation approved.",
        )
    else:
        messages.success(
            request,
            f"Donation review updated to "
            f"{labels.get(donation.qa_status, donation.qa_status)}.",
        )

    if action in ("approve", "reject") and next_id:
        return _redirect_to_donation(batch.id, next_id)
    return _redirect_to_donation(batch.id, donation.id)


def _handle_donor_status_update(
    request: HttpRequest, batch: DonationBatch, donation: Donation
) -> HttpResponse:
    """Apply a donor contact-status change initiated from the QA page.

    Deliberately independent of the donation's qa_status — reviewers can
    mark a donor deceased or gone-away while the current donation's payment
    is still processed normally.
    """
    contact_status = request.POST.get("contact_status", "").strip()
    reason = request.POST.get("contact_status_reason", "").strip()

    valid_statuses = {value for value, _ in Donor.CONTACT_STATUS_CHOICES}
    if contact_status not in valid_statuses:
        messages.error(request, "Select a valid donor contact status.")
        return _redirect_to_donation(batch.id, donation.id)

    donor_attrs: tuple[tuple[str, _DonorModel], ...] = (
        ("donor", Donor),
        ("data_file_donor", DataFileDonor),
        ("system_donor", SystemDonor),
    )
    donor_links: list[tuple[_DonorModel, int]] = []
    for attr, model in donor_attrs:
        linked = getattr(donation, attr, None)
        if linked is None:
            continue
        donor_links.append((model, linked.pk))

    if not donor_links:
        messages.error(
            request,
            "No donor record is linked to this donation — status cannot be updated.",
        )
        return _redirect_to_donation(batch.id, donation.id)

    apply_donor_contact_status(
        donor_links, contact_status=contact_status, reason=reason
    )

    label = dict(Donor.CONTACT_STATUS_CHOICES).get(contact_status, contact_status)
    messages.success(request, f"Donor contact status updated to {label}.")
    return _redirect_to_donation(batch.id, donation.id)


def _handle_review_post(
    request: HttpRequest,
    batch: DonationBatch,
    donation: Donation,
    next_id: str | None,
) -> HttpResponse:
    """Process POST for saving donation edits or QA status actions."""
    if "save_donation" in request.POST:
        return _handle_save_donation(request, batch, donation)
    if "save_donor_status" in request.POST:
        return _handle_donor_status_update(request, batch, donation)
    return _handle_qa_action(request, batch, donation, next_id)


@qa_access_required
@has_permission_or_is_staff("change_donation")
@require_http_methods(["POST"])
def qa_resolve_pending_donor(
    request: HttpRequest, batch_id: int, donation_id: str
) -> HttpResponse:
    """Confirm or reject a scan-pipeline auto-created system donor.

    ``confirm`` flips ``pending_review`` to ``False`` so the donor is treated
    as a real house-file record. ``reject`` disconnects the donor from this
    placeholder and donation; if no other donations still reference the
    pending-review donor it is deleted to avoid junk house-file rows.
    """
    action = request.POST.get("action", "").strip().lower()
    if action not in {"confirm", "reject"}:
        messages.error(request, "Invalid pending-donor action.")
        return _redirect_to_donation(batch_id, donation_id)

    donation = get_object_or_404(
        Donation.objects.select_related("system_donor", "scan_placeholder"),
        id=donation_id,
        batch_id=batch_id,
    )
    system_donor = donation.system_donor
    if system_donor is None or not system_donor.pending_review:
        messages.error(
            request,
            "This donation has no pending-review donor awaiting confirmation.",
        )
        return _redirect_to_donation(batch_id, donation_id)

    placeholder = getattr(donation, "scan_placeholder", None)
    with transaction.atomic():
        if action == "confirm":
            system_donor.pending_review = False
            system_donor.save(update_fields=["pending_review", "updated_at"])
            messages.success(
                request,
                f"Donor {system_donor.full_name} confirmed and added to the "
                "house file.",
            )
        else:
            donor_pk = system_donor.pk
            # Lock the SystemDonor row for the duration of the transaction so
            # no parallel txn can attach a new Donation to it between our
            # disconnect and orphan-cleanup steps. If the donor was already
            # resolved (e.g. by another QA reviewer) bail out — the message
            # below still applies because our donation has been disconnected
            # only inside this txn, and we abort before any save runs.
            locked_donor = (
                _lock_self(SystemDonor.objects)
                .filter(pk=donor_pk, pending_review=True)
                .first()
            )
            if locked_donor is None:
                messages.error(
                    request,
                    "Pending donor was already resolved by another reviewer.",
                )
                return _redirect_to_donation(batch_id, donation_id)

            donation.system_donor = None
            donation.save(update_fields=["system_donor", "updated_at"])
            if placeholder is not None:
                placeholder_fields: list[str] = []
                if placeholder.matched_system_donor_id == donor_pk:
                    placeholder.matched_system_donor = None
                    placeholder_fields.append("matched_system_donor")
                # Also clear the legacy Donor FK so it can't dangle if the
                # auto-create path ever populated it for the rejected donor.
                if placeholder.matched_donor_id is not None:
                    placeholder.matched_donor = None
                    placeholder_fields.append("matched_donor")
                if placeholder_fields:
                    placeholder_fields.append("updated_at")
                    placeholder.save(update_fields=placeholder_fields)
            other_refs = Donation.objects.filter(system_donor=locked_donor).exists()
            if not other_refs:
                locked_donor.delete()
            messages.success(
                request,
                "Pending donor rejected and disconnected from this donation.",
            )

    log_request_action(
        request,
        action="UPDATE" if action == "confirm" else "DELETE",
        model_name="SystemDonor",
        object_id=str(system_donor.pk),
        object_repr=str(system_donor),
        summary=f"QA {action} pending-review donor for donation {donation.id}",
        changes={"action": action, "donation_id": str(donation.id)},
    )
    return _redirect_to_donation(batch_id, donation_id)


def _parse_redaction_pages(
    raw_pages: object, *, expected_count: int, label: str
) -> tuple[list[list[dict[str, float]]], JsonResponse | None]:
    """Validate one of the JSON-payload page-rectangle arrays.

    Returns ``(parsed_rects, None)`` on success or ``([], error_response)``
    on validation failure. *label* is the field name shown in error
    messages (e.g. ``"pages"`` or ``"cvv_pages"``).
    """
    if not isinstance(raw_pages, list) or len(raw_pages) != expected_count:
        return [], JsonResponse(
            {
                "success": False,
                "error": (
                    f"Provide '{label}' rectangles for exactly "
                    f"{expected_count} page(s)."
                ),
            },
            status=400,
        )

    indexed: list[tuple[int, list[dict[str, float]]]] = []
    seen: set[int] = set()
    for entry in raw_pages:
        if not isinstance(entry, dict):
            return [], JsonResponse(
                {"success": False, "error": f"Invalid '{label}' page entry."},
                status=400,
            )
        index = entry.get("page_index")
        rects = entry.get("rects", [])
        if not isinstance(index, int) or index < 0 or index >= expected_count:
            return [], JsonResponse(
                {
                    "success": False,
                    "error": f"Invalid '{label}' page_index value.",
                },
                status=400,
            )
        if index in seen:
            return [], JsonResponse(
                {
                    "success": False,
                    "error": f"Duplicate '{label}' page_index.",
                },
                status=400,
            )
        if not isinstance(rects, list) or not all(isinstance(r, dict) for r in rects):
            return [], JsonResponse(
                {
                    "success": False,
                    "error": f"'{label}' rects must be a list of objects.",
                },
                status=400,
            )
        seen.add(index)
        indexed.append((index, cast(list[dict[str, float]], rects)))

    indexed.sort(key=lambda item: item[0])
    return [rects for _, rects in indexed], None


@qa_access_required
@has_permission_or_is_staff("change_donation")
@require_http_methods(["POST"])
def qa_save_scan_redaction(
    request: HttpRequest, batch_id: int, donation_id: str
) -> JsonResponse:
    """Persist QA-authored redacted scan pages for a donation.

    Body must be JSON of the form::

        {
          "redaction_notes": "...",
          "cvv_pages": [
            {"page_index": 0, "rects": [{"x":..,"y":..,"width":..,"height":..}]},
            ...
          ],
          "pages": [
            {"page_index": 0, "rects": [{...PAN, expiry, signature, ...}]},
            ...
          ]
        }

    The browser sends two rectangle sets:

    * ``cvv_pages`` — applied to R2 immediately on the next Stripe
      authorization attempt by ``apply_cvv_redaction_task``. PCI DSS
      Requirement 3.2 forbids storing sensitive authentication data
      after authorization, so the CVV box must be removed even when
      the charge is declined.
    * ``pages`` — applied once the donation is settled (charge
      succeeded, operator rejected, or retention TTL expired). Stays
      readable across declined retries so operators can re-read the PAN
      with a fresh card.

    For card payment methods, both sets are required; ``cvv_pages`` may
    only be empty for non-card methods that don't have a CVV box.
    """
    from scans.scan_redaction import (
        expected_redaction_page_count,
        manual_redaction_required_for,
        save_deferred_redaction_coords,
    )

    donation = get_object_or_404(
        Donation.objects.select_related("scan_placeholder", "batch"),
        id=donation_id,
        batch_id=batch_id,
    )
    placeholder = getattr(donation, "scan_placeholder", None)
    if placeholder is None:
        return JsonResponse(
            {"success": False, "error": "This donation has no scanned form to redact."},
            status=404,
        )

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except UnicodeDecodeError, json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON body"}, status=400
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {"success": False, "error": "Invalid JSON body"}, status=400
        )

    redaction_notes = str(payload.get("redaction_notes", "") or "")
    expected = expected_redaction_page_count(placeholder)
    raw_post_charge = payload.get("pages")
    raw_cvv = payload.get(
        "cvv_pages", [{"page_index": i, "rects": []} for i in range(expected)]
    )

    post_charge_rects, err = _parse_redaction_pages(
        raw_post_charge, expected_count=expected, label="pages"
    )
    if err is not None:
        return err

    cvv_rects, err = _parse_redaction_pages(
        raw_cvv, expected_count=expected, label="cvv_pages"
    )
    if err is not None:
        return err

    payment_method = donation.payment_method or ""
    requires_cvv_mark = (
        payment_method in _CARD_PAYMENT_METHODS
        and manual_redaction_required_for(payment_method)
        and not any(rects for rects in cvv_rects)
    )
    if requires_cvv_mark:
        # Card payment method requires explicit CVV rectangles. PCI DSS
        # 3.2 forbids the CVV from being kept after authorization, and
        # the only way to honour that here is for QA to mark the box.
        return JsonResponse(
            {
                "success": False,
                "error": (
                    "Card donations require at least one CVV rectangle "
                    "(mark the CVV box on the scanned form before saving)."
                ),
            },
            status=400,
        )

    try:
        save_deferred_redaction_coords(
            placeholder,
            cvv_page_rects=cvv_rects,
            post_charge_page_rects=post_charge_rects,
            user=cast(User, request.user),
            redaction_notes=redaction_notes,
        )
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    except Exception:
        logger.exception("QA redaction save failed for placeholder %s", placeholder.id)
        return JsonResponse(
            {
                "success": False,
                "error": "Redaction could not be saved. See server logs for details.",
            },
            status=500,
        )

    log_request_action(
        request,
        action="UPDATE",
        model_name="ScanPlaceholder",
        object_id=str(placeholder.id),
        object_repr=str(placeholder),
        summary="Saved QA scan redaction",
        changes={
            "redaction_status": placeholder.redaction_status,
            "page_count": len(placeholder.page_keys or []),
            "donation_id": str(donation.id),
        },
    )

    return JsonResponse(
        {
            "success": True,
            "image_url": DonationScanService.get_scanned_form_url(
                donation,
                require_existing=True,
                user=cast(User, request.user),
                allow_pending_redaction=True,
            ),
            "page_urls": DonationScanService.get_scanned_form_page_urls(
                donation,
                require_existing=True,
                user=cast(User, request.user),
                allow_pending_redaction=True,
            ),
            "redaction_status": placeholder.redaction_status,
            "redaction_notes": placeholder.redaction_notes,
        }
    )


def _preload_form_images(
    donations_qs: QuerySet[Donation],
    donation_ids: list[str],
    position: int,
    *,
    user: User | None = None,
) -> list[dict[str, object]]:
    """Pre-load scanned form images for adjacent donations.

    Args:
        donations_qs: QuerySet of donations.
        donation_ids: Ordered list of donation IDs.
        position: Current donation's zero-based index.

    Returns:
        List of ``{"id": ..., "url": ...}`` dicts.
    """
    total = len(donation_ids)
    candidates: list[str] = []

    if position + 1 < total:
        candidates.append(donation_ids[position + 1])
    if position + 2 < total:
        candidates.append(donation_ids[position + 2])
    if position > 0:
        candidates.append(donation_ids[position - 1])

    images: list[dict[str, object]] = []
    if candidates:
        for d in donations_qs.filter(id__in=candidates):
            url = DonationScanService.get_scanned_form_url(
                d,
                require_existing=True,
                user=user,
                allow_pending_redaction=True,
            )
            if url:
                images.append({"id": d.id, "url": url})
    return images


# ---------------------------------------------------------------------------
# Batch status management
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
@has_permission_or_is_staff("change_donationbatch")
@require_http_methods(["POST"])
def qa_update_batch_status(request: HttpRequest, batch_id: int) -> HttpResponse:
    """Update QA status of a donation batch via the status dropdown."""
    batch = get_object_or_404(DonationBatch, id=batch_id)
    new_status = request.POST.get("status")
    review_notes = request.POST.get("review_notes", "").strip()

    valid_statuses = [
        DonationBatch.STATUS_PENDING_QA,
        DonationBatch.STATUS_IN_REVIEW,
        DonationBatch.STATUS_APPROVED,
        DonationBatch.STATUS_REJECTED,
    ]
    if new_status not in valid_statuses:
        messages.error(request, "Invalid status selected.")
        return redirect("custom_admin:qa_batch_review", batch_id=batch_id)

    if new_status == DonationBatch.STATUS_APPROVED:
        blocked = _batch_approval_blocked_response(request, batch_id, batch)
        if blocked is not None:
            return blocked

    old_status = batch.status
    batch.status = new_status  # type: ignore[assignment]
    batch.reviewed_by = request.user  # type: ignore[assignment]
    batch.reviewed_at = timezone.now()
    if review_notes:
        batch.review_notes = review_notes

    committed = _commit_batch_status(
        request,
        batch,
        summary=f"QA status changed from {old_status} to {new_status} for batch {batch.batch_name}",
        changes={
            "old_status": old_status,
            "new_status": new_status,
            "review_notes": review_notes,
        },
    )

    status_labels = {
        DonationBatch.STATUS_PENDING_QA: "Pending QA",
        DonationBatch.STATUS_IN_REVIEW: "In Review",
        DonationBatch.STATUS_APPROVED: "Approved",
        DonationBatch.STATUS_REJECTED: "Rejected",
    }
    if committed:
        messages.success(
            request,
            f'Batch "{batch.batch_name}" status updated to '
            f"{status_labels.get(new_status, new_status)}.",
        )
    else:
        messages.info(
            request,
            f'Batch "{batch.batch_name}" is already '
            f"{status_labels.get(new_status, new_status)}.",
        )

    if new_status == DonationBatch.STATUS_APPROVED:
        return redirect("custom_admin:qa_dashboard")
    return redirect("custom_admin:qa_batch_review", batch_id=batch_id)


@qa_access_required
def qa_batch_stats_api(request: HttpRequest) -> JsonResponse:
    """API endpoint returning real-time QA statistics.

    Args:
        request: HTTP request.

    Returns:
        JSON response with statistics.
    """
    stats = {
        "pending_qa": DonationBatch.objects.filter(
            status=DonationBatch.STATUS_PENDING_QA
        ).count(),
        "in_review": DonationBatch.objects.filter(
            status=DonationBatch.STATUS_IN_REVIEW
        ).count(),
        "approved_today": DonationBatch.objects.filter(
            status=DonationBatch.STATUS_APPROVED,
            reviewed_at__date=timezone.now().date(),
        ).count(),
        "rejected": DonationBatch.objects.filter(
            status=DonationBatch.STATUS_REJECTED
        ).count(),
        "total_pending": DonationBatch.objects.filter(
            status__in=[
                DonationBatch.STATUS_PENDING_QA,
                DonationBatch.STATUS_IN_REVIEW,
            ]
        ).count(),
    }
    return JsonResponse(stats)


# ---------------------------------------------------------------------------
# HTMX: assign a house-file donor to a null-donor donation (warm QR failures)
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
@has_permission_or_is_staff("change_donation")
@require_http_methods(["POST"])
def htmx_assign_donor_to_donation(
    request: HttpRequest, donation_id: str
) -> HttpResponse:
    """Assign an existing house-file donor to a donation that has no donor.

    Used during QA review when a warm-campaign QR code could not be matched
    and the donation was created without a donor link.  The QA reviewer
    searches for the correct donor and posts their UUID pk here.

    Args:
        request: HTTP POST request with ``donor_pk`` in the body.
        donation_id: UUID of the donation to update.

    Returns:
        HTMX redirect back to the same QA review page.
    """
    donation = get_object_or_404(
        Donation.objects.select_related("batch"),
        id=donation_id,
    )
    donor_pk = request.POST.get("donor_pk", "").strip()
    if not donor_pk:
        return JsonResponse(
            {"success": False, "error": "No donor selected."}, status=400
        )

    donor = get_object_or_404(Donor, id=donor_pk)
    system_donor = upsert_system_donor_from_source(
        donation.campaign,
        donor=donor,
        created_by=request.user,
    )
    donation.donor = None
    donation.data_file_donor = None
    donation.system_donor = system_donor
    # Re-open for review so the QA agent sees the updated record.
    if donation.qa_status not in (
        Donation.QA_STATUS_APPROVED,
        Donation.QA_STATUS_REJECTED,
    ):
        donation.qa_status = Donation.QA_STATUS_PENDING
    donation.save(
        update_fields=[
            "donor",
            "data_file_donor",
            "system_donor",
            "qa_status",
            "updated_at",
        ]
    )

    log_request_action(
        request,
        action="UPDATE",
        model_name="Donation",
        object_id=str(donation.id),
        object_repr=str(donation),
        summary=(
            f"Donor manually assigned during QA: {system_donor.full_name} "
            f"(URN: {system_donor.external_urn})"
        ),
        changes={
            "source_donor_pk": str(donor.id),
            "system_donor_pk": str(system_donor.id),
            "system_donor_urn": system_donor.external_urn,
        },
    )

    redirect_url = reverse(
        "custom_admin:qa_single_donation_review",
        args=[donation.batch_id, donation_id],
    )
    response = HttpResponse(status=204)
    response["HX-Redirect"] = redirect_url
    return response


# ---------------------------------------------------------------------------
# 3DS / SCA authentication link dispatch
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
@has_permission_or_is_staff("change_donation")
@require_http_methods(["POST"])
def qa_send_authentication_link(
    request: HttpRequest, batch_id: int, donation_id: str
) -> HttpResponse:
    """Email a Stripe Checkout link to the donor for 3DS / SCA recovery.

    Triggered from the QA donation review screen when a card donation is in
    ``payment_status="awaiting_authentication"``. Delegates to
    :meth:`StripePaymentService.send_authentication_link_for_donation` which
    creates a fresh Checkout session, cancels the original requires_action
    PaymentIntent, repoints the local ``StripePayment`` row at the new
    Checkout-owned PaymentIntent, and emails the donor a one-time link.

    Args:
        request: HTTP POST request.
        batch_id: Primary key of the DonationBatch.
        donation_id: UUID of the donation in ``awaiting_authentication``.

    Returns:
        Redirect back to the QA donation review page with a flash message.
    """
    from django.core.exceptions import ValidationError

    from payments.services import StripePaymentError, StripePaymentService

    donation = get_object_or_404(
        Donation.objects.select_related(
            "campaign",
            "campaign__client",
            "donor",
            "data_file_donor",
            "system_donor",
        ),
        id=donation_id,
        batch_id=batch_id,
    )
    if donation.payment_status != Donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION:
        messages.error(
            request,
            "This donation is not awaiting 3DS / SCA authentication.",
        )
        return _redirect_to_donation(batch_id, donation_id)

    base_url = (getattr(settings, "INSYTE_PUBLIC_BASE_URL", "") or "").rstrip("/")
    return_path = reverse(
        "custom_admin:qa_single_donation_review",
        args=[batch_id, donation_id],
    )
    success_url = f"{base_url}{return_path}" if base_url else return_path
    cancel_url = success_url

    try:
        result = StripePaymentService.send_authentication_link_for_donation(
            donation,
            success_url=success_url,
            cancel_url=cancel_url,
            user=cast(User, request.user),
        )
    except ValidationError as exc:
        message = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
        messages.error(request, message)
        return _redirect_to_donation(batch_id, donation_id)
    except StripePaymentError as exc:
        messages.error(request, str(exc))
        return _redirect_to_donation(batch_id, donation_id)

    log_request_action(
        request,
        action="UPDATE",
        model_name="Donation",
        object_id=str(donation.id),
        object_repr=str(donation),
        summary="Sent 3DS/SCA authentication link to donor",
        changes={
            "donor_email": result.get("donor_email", ""),
            "checkout_session_id": result.get("session_id", ""),
            "stripe_payment_id": result.get("payment_id", ""),
        },
    )

    messages.success(
        request,
        f"Authentication link sent to {result.get('donor_email', 'donor')}.",
    )
    return _redirect_to_donation(batch_id, donation_id)


# ---------------------------------------------------------------------------
# Gift Aid report download
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
@require_http_methods(["GET"])
def download_gift_aid_report(request: HttpRequest, batch_id: int) -> FileResponse:
    """Serve the auto-generated Gift Aid CSV for a batch.

    The CSV is written to ``MEDIA_ROOT/gift_aid_reports/`` by
    ``on_batch_approved_task`` (via ``_run_gift_aid_for_batch``) after a batch
    is approved.

    Args:
        request: HTTP GET request.
        batch_id: Primary key of the DonationBatch.

    Returns:
        FileResponse streaming the CSV file.

    Raises:
        Http404: If the batch has no gift aid report path recorded or the
            file does not exist on disk.
    """
    import os

    batch = get_object_or_404(DonationBatch, id=batch_id)
    relative_path: str = batch.gift_aid_report_path or ""
    if not relative_path:
        raise Http404("Gift Aid report not available for this batch.")

    # Prevent path traversal
    safe_rel = normalize_media_storage_name(relative_path)
    if not safe_rel:
        raise Http404("Invalid report path.")

    if not default_storage.exists(safe_rel):
        raise Http404("Gift Aid report file not found. It may still be generating.")

    filename = os.path.basename(safe_rel)
    return FileResponse(
        open_media_storage_file(safe_rel),
        as_attachment=True,
        filename=filename,
        content_type="text/csv",
    )


@is_authenticated_and_is_staff
@has_permission_or_is_staff("change_donationbatch")
@require_http_methods(["POST"])
def mark_gift_aid_submitted(request: HttpRequest, batch_id: int) -> HttpResponse:
    """Record that the Gift Aid CSV for this batch has been handed to HMRC.

    Sets ``DonationBatch.gift_aid_submitted_at`` so the banking-reversal
    cascade can decide when an HMRC retraction warning is actually warranted.
    Idempotent: a second submission for an already-marked batch is a no-op.

    Always redirects to the batch's QA review page on success/no-op so the
    operator lands somewhere predictable. We do *not* trust ``HTTP_REFERER``
    for the redirect target — it's attacker-controllable, and even with CSRF
    protection in place an open-redirect via ``Referer`` would let a phishing
    flow bounce a staff user back to a malicious origin.

    Args:
        request: HTTP POST request.
        batch_id: Primary key of the DonationBatch.
    """
    batch = get_object_or_404(DonationBatch, id=batch_id)
    return_url = reverse("custom_admin:qa_batch_review", kwargs={"batch_id": batch.id})

    if not batch.gift_aid_report_path:
        messages.error(
            request,
            "This batch has no Gift Aid CSV — nothing to mark as submitted.",
        )
        return redirect(return_url)

    if batch.gift_aid_submitted_at is not None:
        messages.info(
            request,
            (
                f"Gift Aid CSV for batch '{batch.batch_name}' was already "
                f"marked submitted on {batch.gift_aid_submitted_at:%Y-%m-%d %H:%M}."
            ),
        )
        return redirect(return_url)

    batch.gift_aid_submitted_at = timezone.now()
    batch.save(update_fields=["gift_aid_submitted_at", "updated_at"])

    log_request_action(
        request,
        action="UPDATE",
        model_name="DonationBatch",
        object_id=str(batch.id),
        object_repr=batch.batch_name,
        summary="Marked Gift Aid CSV as submitted to HMRC",
        changes={
            "gift_aid_submitted_at": {
                "old": None,
                "new": batch.gift_aid_submitted_at.isoformat(),
            }
        },
    )

    messages.success(
        request,
        (
            f"Gift Aid CSV for batch '{batch.batch_name}' marked submitted to HMRC. "
            "Banking reversals on this batch will now flag HMRC retraction."
        ),
    )
    return redirect(return_url)
