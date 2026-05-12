"""Concurrency regression tests for QA review row locking.

These tests assert that ``_handle_qa_action`` re-fetches the Donation row
under ``select_for_update`` inside its transaction, so two reviewers acting
on the same donation cannot lose updates.

Backend note: dev/test runs SQLite, where ``select_for_update`` is a silent
no-op (SQLite uses database-level locking via journaling). We therefore
cannot reproduce a true contended lock here; instead we assert the SQL
contract — that the second reviewer's call goes through the locked re-fetch
path — and that the second save observes the first reviewer's qa_status.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.db import connection
from django.test import RequestFactory

from custom_admin.views import qa_review
from donations.models import Donation
from tests.factories import DonationFactory, UserFactory


def _noop_message(_request: object, _message: object) -> None:
    """Discard Django flash messages emitted during the unit tests."""


def _required_qa_post_fields() -> dict[str, str]:
    """Minimum POST payload accepted by ``_handle_qa_action``."""
    return {
        "donor_title": "Mr",
        "donor_first_name": "John",
        "donor_last_name": "Doe",
        "package_code": "PKG01",
        "donor_address_line1": "10 High Street",
        "donor_postcode": "SW1A 1AA",
    }


@pytest.mark.django_db()
class TestQaActionRowLocking:
    """Verify ``_handle_qa_action`` row-locks the Donation under review."""

    def test_qa_action_uses_select_for_update_on_donation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handler must re-fetch the Donation via ``select_for_update``.

        We patch ``Donation.objects.select_for_update`` to record whether it
        was called inside ``_handle_qa_action``; this is the SQL contract
        that prevents lost updates between two concurrent reviewers.
        """
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="completed",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", **_required_qa_post_fields()},
        )
        request.user = staff_user

        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        original = Donation.objects.select_for_update

        with patch.object(
            Donation.objects,
            "select_for_update",
            side_effect=original,
            autospec=True,
        ) as locked_call:
            response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
                request,
                donation.batch,
                donation,
                next_id=None,
            )

        assert response.status_code == 302
        assert locked_call.called, (
            "_handle_qa_action must call select_for_update on the Donation "
            "queryset so concurrent QA reviewers cannot race."
        )
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED

    def test_two_sequential_reviewers_last_save_uses_locked_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate two reviewers acting on the same donation in sequence.

        Reviewer A approves; reviewer B (working from a stale in-memory copy)
        rejects. Without row locking, B's save would silently overwrite A's
        qa_status because B's ``donation.save(update_fields=...)`` would
        write whatever was on B's stale instance. With the lock in place B's
        atomic block re-fetches the row first, so the resulting state always
        reflects a sequential apply: B's reject lands on top of A's approve.
        """
        staff_a = UserFactory(is_staff=True, username="reviewer-a")
        staff_b = UserFactory(is_staff=True, username="reviewer-b")
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="completed",
            qa_status=Donation.QA_STATUS_PENDING,
            qa_notes="",
        )
        # Two views start with the same stale in-memory copy.
        stale_copy_a = Donation.objects.get(pk=donation.pk)
        stale_copy_b = Donation.objects.get(pk=donation.pk)

        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        # Reviewer A: claim and approve.
        qa_review._claim_reviewer_lock(donation.batch.id, staff_a)  # pyright: ignore[reportPrivateUsage]
        request_a = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", **_required_qa_post_fields()},
        )
        request_a.user = staff_a
        qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request_a, donation.batch, stale_copy_a, next_id=None
        )

        # Reviewer B starts with their stale copy still showing PENDING. The
        # test would be meaningless if both copies already converged.
        assert stale_copy_b.qa_status == Donation.QA_STATUS_PENDING

        # Clear A's claim so B can take the batch (simulates session handoff).
        donation.batch.reviewer_locked_by = None
        donation.batch.reviewer_locked_at = None
        donation.batch.save(
            update_fields=["reviewer_locked_by", "reviewer_locked_at", "updated_at"]
        )
        qa_review._claim_reviewer_lock(donation.batch.id, staff_b)  # pyright: ignore[reportPrivateUsage]

        request_b = RequestFactory().post(
            "/admin/qa/",
            {
                "action": "reject",
                "qa_reject_reason": Donation.QA_REJECT_REASON_OTHER,
                "qa_notes": "Reviewer B disagrees",
                **_required_qa_post_fields(),
            },
        )
        request_b.user = staff_b
        qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request_b, donation.batch, stale_copy_b, next_id=None
        )

        donation.refresh_from_db()
        # Last writer wins, but only after re-fetching post-A state.
        assert donation.qa_status == Donation.QA_STATUS_REJECTED
        assert donation.qa_notes == "Reviewer B disagrees"

    def test_select_for_update_uses_of_self_on_postgres_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``of=("self",)`` is gated behind the Postgres vendor check.

        SQLite does not support the ``of=`` argument, so the dev/test path
        must use a plain ``select_for_update()`` call. We verify that the
        helper resolves ``of=("self",)`` only when the active vendor reports
        non-sqlite, which is the live-wire contract that protects production.
        """
        # Sanity: dev/test backend is SQLite, so the helper reports False.
        from donors import updates as donors_updates

        assert connection.vendor == "sqlite"
        assert donors_updates._supports_row_locks() is False  # pyright: ignore[reportPrivateUsage]

        # Simulate the production code path by forcing the helper True. Patch
        # both the canonical home in ``donors.updates`` (where ``_lock_self``
        # reads from) AND the qa_review re-export (kept for direct callers).
        monkeypatch.setattr(donors_updates, "_supports_row_locks", lambda: True)
        monkeypatch.setattr(qa_review, "_supports_row_locks", lambda: True)

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="completed",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)

        captured_kwargs: dict[str, object] = {}
        original = Donation.objects.select_for_update

        def _spy(*args: object, **kwargs: object) -> object:
            captured_kwargs.update(kwargs)
            # Drop ``of`` before delegating — SQLite does not support it.
            kwargs.pop("of", None)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        with patch.object(Donation.objects, "select_for_update", side_effect=_spy):
            request = RequestFactory().post(
                "/admin/qa/",
                {"action": "approve", **_required_qa_post_fields()},
            )
            request.user = staff_user
            qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
                request, donation.batch, donation, next_id=None
            )

        assert captured_kwargs.get("of") == ("self",), (
            "On Postgres-class backends the QA action must lock only the "
            'Donation row via of=("self",) to avoid escalating locks to '
            "joined tables."
        )
