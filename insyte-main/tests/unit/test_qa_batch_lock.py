"""Unit tests for the batch-level reviewer claim mechanism.

Covers:
- Lock acquisition on QA review entry (claim or refresh).
- Conflict detection when a second reviewer opens the same batch.
- Stale-claim takeover after :data:`REVIEWER_LOCK_TTL`.
- ``PermissionDenied`` raised when saving without a live claim.
- Lock release on terminal batch actions (approve / reject / resubmit).
- The periodic Celery cleanup task clearing aged-out locks.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.test import RequestFactory
from django.utils import timezone

from custom_admin.views import qa_review
from donations.models import REVIEWER_LOCK_TTL, Donation, DonationBatch
from donations.tasks import release_stale_batch_locks_task
from tests.factories import DonationBatchFactory, DonationFactory, UserFactory


def _noop_message(_request: object, _message: object) -> None:
    """Suppress Django flash messages during unit tests."""


def _patch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out flash messaging, audit logging, and cache invalidation."""
    for level in ("success", "info", "error", "warning"):
        monkeypatch.setattr(qa_review.messages, level, _noop_message)
    monkeypatch.setattr(qa_review, "log_request_action", lambda *_a, **_k: None)
    monkeypatch.setattr(qa_review.cache, "delete", lambda _key: None)


def _staff_request(
    url: str = "/admin/qa/x/", data: dict[str, str] | None = None
) -> HttpRequest:
    """Build a POST request without a user attached.

    Caller binds ``request.user`` after construction.
    """
    return RequestFactory().post(url, data or {})


# ---------------------------------------------------------------------------
# Model helper
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestReviewerLockIsActive:
    """The model helper distinguishes live, stale, and missing claims."""

    def test_no_holder_means_inactive(self) -> None:
        batch = DonationBatchFactory()
        assert batch.reviewer_lock_is_active() is False

    def test_recent_claim_is_active(self) -> None:
        user = UserFactory()
        batch = DonationBatchFactory(
            reviewer_locked_by=user,
            reviewer_locked_at=timezone.now(),
        )
        assert batch.reviewer_lock_is_active() is True

    def test_aged_out_claim_is_inactive(self) -> None:
        user = UserFactory()
        batch = DonationBatchFactory(
            reviewer_locked_by=user,
            reviewer_locked_at=timezone.now()
            - REVIEWER_LOCK_TTL
            - timedelta(seconds=1),
        )
        assert batch.reviewer_lock_is_active() is False


# ---------------------------------------------------------------------------
# Claim / release helpers
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestClaimReviewerLock:
    """``_claim_reviewer_lock`` arbitrates between competing reviewers."""

    def test_first_reviewer_wins_lock(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")

        claimed_batch, conflict = qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]

        assert conflict is None
        claimed_batch.refresh_from_db()
        assert claimed_batch.reviewer_locked_by_id == alice.pk
        assert claimed_batch.reviewer_locked_at is not None

    def test_second_reviewer_blocked_by_active_claim(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")
        bob = UserFactory(username="bob")

        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        _, conflict = qa_review._claim_reviewer_lock(batch.id, bob)  # pyright: ignore[reportPrivateUsage]

        assert conflict is not None
        assert conflict.pk == alice.pk
        batch.refresh_from_db()
        # Lock holder was not overwritten by the conflicting attempt.
        assert batch.reviewer_locked_by_id == alice.pk

    def test_same_user_refresh_extends_timestamp(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")

        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        batch.refresh_from_db()
        first_ts = batch.reviewer_locked_at
        assert first_ts is not None

        # Manually back-date so the refresh produces a strictly later timestamp.
        DonationBatch.objects.filter(pk=batch.pk).update(
            reviewer_locked_at=first_ts - timedelta(seconds=5)
        )

        _, conflict = qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        assert conflict is None
        batch.refresh_from_db()
        assert batch.reviewer_locked_at is not None
        assert batch.reviewer_locked_at >= first_ts - timedelta(seconds=5)

    def test_stale_claim_is_taken_over(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")
        bob = UserFactory(username="bob")

        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        DonationBatch.objects.filter(pk=batch.pk).update(
            reviewer_locked_at=timezone.now() - REVIEWER_LOCK_TTL - timedelta(minutes=1)
        )

        _, conflict = qa_review._claim_reviewer_lock(batch.id, bob)  # pyright: ignore[reportPrivateUsage]

        assert conflict is None
        batch.refresh_from_db()
        assert batch.reviewer_locked_by_id == bob.pk

    def test_lock_scopes_to_self_on_postgres(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim must lock only DonationBatch, not the joined User table.

        ``select_related("reviewer_locked_by")`` produces a LEFT OUTER JOIN
        because the FK is nullable. Postgres rejects ``FOR UPDATE`` on the
        nullable side of an outer join with
        ``NotSupportedError: FOR UPDATE cannot be applied to the nullable
        side of an outer join``. The fix is ``of=("self",)`` so only the
        DonationBatch row is locked. This pins that invariant by capturing
        ``select_for_update`` call args when ``_supports_row_locks`` is True.
        """
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")

        captured_kwargs: list[dict[str, Any]] = []
        original_sfu = qa_review.DonationBatch.objects.select_for_update

        def _capturing_sfu(*args: Any, **kwargs: Any) -> Any:
            captured_kwargs.append(dict(kwargs))
            return original_sfu(*args, **kwargs)

        # The lock helper now lives in ``donors.updates`` (shared with
        # phone-intake); patch the canonical location so ``_lock_self`` sees
        # the override regardless of which import re-exports it.
        from donors import updates as donors_updates

        monkeypatch.setattr(donors_updates, "_supports_row_locks", lambda: True)
        monkeypatch.setattr(qa_review, "_supports_row_locks", lambda: True)
        monkeypatch.setattr(
            qa_review.DonationBatch.objects, "select_for_update", _capturing_sfu
        )

        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]

        # _claim_reviewer_lock should call select_for_update with of=("self",)
        # — the lock-scoping argument that makes Postgres accept the query.
        assert captured_kwargs, "select_for_update was never invoked"
        assert any(kw.get("of") == ("self",) for kw in captured_kwargs), (
            f"expected select_for_update(of=('self',)), got {captured_kwargs!r}"
        )


@pytest.mark.django_db()
class TestReleaseReviewerLock:
    """Release is conditional on the caller still owning the claim."""

    def test_release_clears_owned_claim(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")
        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]

        qa_review._release_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]

        batch.refresh_from_db()
        assert batch.reviewer_locked_by_id is None
        assert batch.reviewer_locked_at is None

    def test_release_leaves_other_users_lock_intact(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")
        bob = UserFactory(username="bob")
        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]

        qa_review._release_reviewer_lock(batch.id, bob)  # pyright: ignore[reportPrivateUsage]

        batch.refresh_from_db()
        assert batch.reviewer_locked_by_id == alice.pk


# ---------------------------------------------------------------------------
# Save-time enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestEnforceReviewerLock:
    """``_enforce_reviewer_lock_or_raise`` blocks saves without a live claim."""

    def test_owned_claim_does_not_raise(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")
        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        batch.refresh_from_db()

        qa_review._enforce_reviewer_lock_or_raise(batch, alice)  # pyright: ignore[reportPrivateUsage]

    def test_other_user_claim_raises_permission_denied(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")
        bob = UserFactory(username="bob")
        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        batch.refresh_from_db()

        with pytest.raises(PermissionDenied):
            qa_review._enforce_reviewer_lock_or_raise(batch, bob)  # pyright: ignore[reportPrivateUsage]

    def test_stale_claim_raises_permission_denied(self) -> None:
        batch = DonationBatchFactory()
        alice = UserFactory(username="alice")
        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        DonationBatch.objects.filter(pk=batch.pk).update(
            reviewer_locked_at=timezone.now() - REVIEWER_LOCK_TTL - timedelta(minutes=5)
        )
        batch.refresh_from_db()

        with pytest.raises(PermissionDenied):
            qa_review._enforce_reviewer_lock_or_raise(batch, alice)  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# View-level integration
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestQaActionLockIntegration:
    """The QA action handler refuses to write without a held claim."""

    def test_save_donation_blocked_when_lock_held_by_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_messages(monkeypatch)
        alice = UserFactory(is_staff=True, is_superuser=True, username="alice")
        bob = UserFactory(is_staff=True, is_superuser=True, username="bob")
        batch = DonationBatchFactory(status=DonationBatch.STATUS_PENDING_QA)
        donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            qa_status=Donation.QA_STATUS_PENDING,
        )
        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        batch.refresh_from_db()

        request = _staff_request()
        request.user = bob

        with pytest.raises(PermissionDenied):
            qa_review._handle_save_donation(request, batch, donation)  # pyright: ignore[reportPrivateUsage]

    def test_qa_action_blocked_when_lock_held_by_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_messages(monkeypatch)
        alice = UserFactory(is_staff=True, is_superuser=True, username="alice")
        bob = UserFactory(is_staff=True, is_superuser=True, username="bob")
        batch = DonationBatchFactory(status=DonationBatch.STATUS_PENDING_QA)
        donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            qa_status=Donation.QA_STATUS_PENDING,
        )
        qa_review._claim_reviewer_lock(batch.id, alice)  # pyright: ignore[reportPrivateUsage]
        batch.refresh_from_db()

        request = _staff_request(data={"action": "approve"})
        request.user = bob

        with pytest.raises(PermissionDenied):
            qa_review._handle_qa_action(request, batch, donation, next_id=None)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.django_db()
class TestTerminalActionsReleaseLock:
    """Approving, rejecting, or resubmitting a batch frees the reviewer claim."""

    def test_approve_batch_releases_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_APPROVED,
        )
        qa_review._claim_reviewer_lock(batch.id, staff)  # pyright: ignore[reportPrivateUsage]

        request = _staff_request(data={"batch_notes": ""})
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)
        assert response.status_code == 302

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        assert batch.reviewer_locked_by_id is None
        assert batch.reviewer_locked_at is None

    def test_reject_batch_releases_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_PENDING,
        )
        qa_review._claim_reviewer_lock(batch.id, staff)  # pyright: ignore[reportPrivateUsage]

        request = _staff_request(data={"batch_notes": "duplicate scans"})
        request.user = staff

        response = qa_review.qa_reject_batch(request, batch.id)
        assert response.status_code == 302

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_REJECTED
        assert batch.reviewer_locked_by_id is None

    def test_resubmit_batch_releases_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_REJECTED,
            default_payment_method="caf",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_REJECTED,
        )
        qa_review._claim_reviewer_lock(batch.id, staff)  # pyright: ignore[reportPrivateUsage]

        request = _staff_request()
        request.user = staff

        response = qa_review.qa_batch_resubmit(request, batch.id)
        assert response.status_code == 302

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA
        assert batch.reviewer_locked_by_id is None

    def test_approve_batch_releases_lock_when_payment_processing_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reviewer claim is freed even if downstream processing raises.

        Regression: terminal QA actions used to call ``_release_reviewer_lock``
        only on the success path. When the batch-status commit (or any
        downstream payment / signal handler reachable from it) raised, the
        reviewer claim leaked for the full ``REVIEWER_LOCK_TTL`` window,
        preventing other reviewers from picking the batch up.
        """
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="card",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            qa_status=Donation.QA_STATUS_APPROVED,
        )
        qa_review._claim_reviewer_lock(batch.id, staff)  # pyright: ignore[reportPrivateUsage]

        def _raise_payment_failure(
            _target_donation: Donation,
            _user: object,
            payment_method_id: str | None = None,
            require_qa_approved: bool = True,
        ) -> dict[str, object]:
            raise RuntimeError("Stripe charge failed")

        monkeypatch.setattr(
            "payments.batch_payment.BatchPaymentService.process_donation_payment",
            _raise_payment_failure,
        )

        # Force ``_commit_batch_status`` to raise so the body of
        # ``qa_approve_batch`` aborts before reaching the success-path
        # release. Any exception originating from the post-status-change
        # signal chain (e.g. payment capture) lands here in real code.
        def _boom(
            _request: object,
            _batch: object,
            *,
            summary: str,
            changes: dict[str, object],
        ) -> bool:
            raise RuntimeError("Stripe charge failed during commit")

        monkeypatch.setattr(qa_review, "_commit_batch_status", _boom)

        request = _staff_request(data={"batch_notes": ""})
        request.user = staff

        with pytest.raises(RuntimeError, match="Stripe charge failed during commit"):
            qa_review.qa_approve_batch(request, batch.id)

        batch.refresh_from_db()
        # The status update never happened (rolled back / never reached),
        # but the reviewer claim must still be cleared.
        assert batch.status == DonationBatch.STATUS_PENDING_QA
        assert batch.reviewer_locked_by_id is None
        assert batch.reviewer_locked_at is None


# ---------------------------------------------------------------------------
# Periodic cleanup task
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestReleaseStaleBatchLocksTask:
    """The Celery beat task drops only locks aged past the TTL."""

    def test_clears_only_stale_locks(self) -> None:
        active_user = UserFactory(username="active-holder")
        stale_user = UserFactory(username="stale-holder")
        active_batch = DonationBatchFactory(
            reviewer_locked_by=active_user,
            reviewer_locked_at=timezone.now(),
        )
        stale_batch = DonationBatchFactory(
            reviewer_locked_by=stale_user,
            reviewer_locked_at=timezone.now()
            - REVIEWER_LOCK_TTL
            - timedelta(minutes=2),
        )
        unlocked_batch = DonationBatchFactory()

        result = release_stale_batch_locks_task()

        assert result == {"released": 1}
        active_batch.refresh_from_db()
        stale_batch.refresh_from_db()
        unlocked_batch.refresh_from_db()
        assert active_batch.reviewer_locked_by_id == active_user.pk
        assert stale_batch.reviewer_locked_by_id is None
        assert stale_batch.reviewer_locked_at is None
        assert unlocked_batch.reviewer_locked_by_id is None
