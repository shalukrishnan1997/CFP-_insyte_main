"""Comprehensive unit tests for PayingInSlip (banking) model.

FIN-BANK-UNIT-* test cases covering slip creation, number generation,
recalculate totals, mark_as_processed, and status workflow.
"""

import datetime
import json
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from banking.models import PayingInSlip
from banking.services import BankingService
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    PayingInSlipFactory,
    UserFactory,
)

# ═══════════════════════════════════════════════════════════════
# PayingInSlip — Core Creation
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestPayingInSlipCreation:
    """FIN-BANK-UNIT-001 to 004: Slip creation and defaults."""

    def test_create_slip_with_defaults(self) -> None:
        """FIN-BANK-UNIT-001: Slip created with draft status."""
        slip = PayingInSlipFactory()
        assert slip.pk is not None
        assert slip.status == "draft"
        assert slip.total_amount == Decimal("0.00")
        assert slip.total_items == 0

    def test_slip_number_unique(self) -> None:
        """FIN-BANK-UNIT-002: Slip numbers must be unique."""
        PayingInSlipFactory(slip_number="UNIQUE-001")
        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            PayingInSlipFactory(slip_number="UNIQUE-001")

    def test_slip_linked_to_client(self) -> None:
        """FIN-BANK-UNIT-003: Slip linked to client."""
        client = ClientFactory(name="Banking Client")
        slip = PayingInSlipFactory(client=client)
        assert slip.client.name == "Banking Client"

    def test_slip_payment_type_choices(self) -> None:
        """FIN-BANK-UNIT-004: All payment types accepted."""
        for ptype in [
            "cash",
            "cheque",
            "postal_order",
            "caf",
            "mixed",
        ]:
            slip = PayingInSlipFactory(payment_type=ptype)
            assert slip.payment_type == ptype


# ═══════════════════════════════════════════════════════════════
# PayingInSlip — Status Workflow
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestPayingInSlipStatus:
    """FIN-BANK-UNIT-005 to 008: Status transitions."""

    def test_status_choices_count(self) -> None:
        """FIN-BANK-UNIT-005: Multiple status choices exist."""
        assert len(PayingInSlip.STATUS_CHOICES) >= 6

    def test_status_draft_to_ready(self) -> None:
        """FIN-BANK-UNIT-006: Status transitions draft → ready."""
        slip = PayingInSlipFactory(status="draft")
        slip.status = "ready"
        slip.save()
        slip.refresh_from_db()
        assert slip.status == "ready"

    def test_status_submitted_to_processed(self) -> None:
        """FIN-BANK-UNIT-007: Status transitions submitted → processed."""
        slip = PayingInSlipFactory(status="submitted_to_bank")
        slip.status = "processed"
        slip.save()
        slip.refresh_from_db()
        assert slip.status == "processed"

    def test_can_be_processed_valid_statuses(self) -> None:
        """FIN-BANK-UNIT-008: can_be_processed True for ready/submitted_to_bank."""
        ready = PayingInSlipFactory(status="ready")
        submitted = PayingInSlipFactory(status="submitted_to_bank")
        draft = PayingInSlipFactory(status="draft")

        assert ready.can_be_processed() is True
        assert submitted.can_be_processed() is True
        assert draft.can_be_processed() is False


# ═══════════════════════════════════════════════════════════════
# PayingInSlip — Slip Number Generation
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestSlipNumberGeneration:
    """FIN-BANK-UNIT-009 to 011: generate_slip_number method."""

    def test_generate_slip_number_format(self) -> None:
        """FIN-BANK-UNIT-009: Slip number follows CLIENT-YYYYMMDD-SEQ format."""
        client = ClientFactory(name="ABC Charity")
        banking_date = datetime.date(2026, 2, 25)
        slip_number = BankingService.generate_slip_number(client, banking_date)
        assert slip_number == "ABC-20260225-001"

    def test_generate_slip_number_sequential(self) -> None:
        """FIN-BANK-UNIT-010: Sequential numbering for same client+date."""
        client = ClientFactory(name="DEF Foundation")
        banking_date = datetime.date(2026, 2, 25)

        # Create first slip
        first_number = BankingService.generate_slip_number(client, banking_date)
        PayingInSlipFactory(
            slip_number=first_number, client=client, banking_date=banking_date
        )

        # Generate second
        second_number = BankingService.generate_slip_number(client, banking_date)
        assert second_number == "DEF-20260225-002"

    def test_generate_slip_number_different_dates(self) -> None:
        """FIN-BANK-UNIT-011: Different dates restart sequence."""
        client = ClientFactory(name="GHI Trust")
        date1 = datetime.date(2026, 2, 25)
        date2 = datetime.date(2026, 2, 26)

        num1 = BankingService.generate_slip_number(client, date1)
        PayingInSlipFactory(slip_number=num1, client=client, banking_date=date1)

        num2 = BankingService.generate_slip_number(client, date2)
        assert num2 == "GHI-20260226-001"


# ═══════════════════════════════════════════════════════════════
# PayingInSlip — Recalculate Totals
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestSlipRecalculateTotals:
    """FIN-BANK-UNIT-012 to 013: recalculate_totals method."""

    def test_recalculate_with_donations(self) -> None:
        """FIN-BANK-UNIT-012: Totals match linked donations."""
        slip = PayingInSlipFactory()
        # Create donations linked to this slip. Cheque payments do not depend
        # on Stripe, so payment_status is irrelevant for them.
        DonationFactory(
            amount=Decimal("100.00"),
            paying_in_slip=slip,
            payment_method="cheque",
        )
        DonationFactory(
            amount=Decimal("250.50"),
            paying_in_slip=slip,
            payment_method="cheque",
        )

        BankingService.recalculate_slip_totals(slip)
        slip.refresh_from_db()
        assert slip.total_amount == Decimal("350.50")
        assert slip.total_items == 2

    def test_recalculate_empty_slip(self) -> None:
        """FIN-BANK-UNIT-013: Empty slip has zero totals."""
        slip = PayingInSlipFactory()
        BankingService.recalculate_slip_totals(slip)
        slip.refresh_from_db()
        assert slip.total_amount == 0
        assert slip.total_items == 0

    def test_recalculate_excludes_unsettled_card_donations(self) -> None:
        """FIN-BANK-UNIT-013a: Pending/failed card donations don't inflate totals."""
        slip = PayingInSlipFactory()
        DonationFactory(
            amount=Decimal("10.00"),
            paying_in_slip=slip,
            payment_method="cheque",
        )
        DonationFactory(
            amount=Decimal("100.00"),
            paying_in_slip=slip,
            payment_method="card",
            payment_status="pending",
        )
        DonationFactory(
            amount=Decimal("200.00"),
            paying_in_slip=slip,
            payment_method="card",
            payment_status="failed",
        )

        BankingService.recalculate_slip_totals(slip)
        slip.refresh_from_db()
        assert slip.total_amount == Decimal("10.00")
        assert slip.total_items == 1

    def test_recalculate_includes_completed_card_donations(self) -> None:
        """FIN-BANK-UNIT-013b: Completed card donations count toward totals."""
        slip = PayingInSlipFactory()
        DonationFactory(
            amount=Decimal("10.00"),
            paying_in_slip=slip,
            payment_method="cheque",
        )
        DonationFactory(
            amount=Decimal("50.00"),
            paying_in_slip=slip,
            payment_method="card",
            payment_status="completed",
        )

        BankingService.recalculate_slip_totals(slip)
        slip.refresh_from_db()
        assert slip.total_amount == Decimal("60.00")
        assert slip.total_items == 2


# ═══════════════════════════════════════════════════════════════
# PayingInSlip — Mark As Processed
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestSlipMarkAsProcessed:
    """FIN-BANK-UNIT-014 to 017: mark_as_processed method."""

    def test_mark_full_success(self) -> None:
        """FIN-BANK-UNIT-014: Full success sets status to 'processed'."""
        slip = PayingInSlipFactory(
            status="submitted_to_bank", total_amount=Decimal("500.00")
        )
        user = UserFactory()

        BankingService.mark_slip_as_processed(
            slip,
            processed_amount=Decimal("500.00"),
            completion_status="full_success",
            bank_processed_date=datetime.date(2026, 2, 26),
            processed_by=user,
        )
        slip.refresh_from_db()
        assert slip.status == "processed"
        assert slip.processed_amount == Decimal("500.00")
        assert slip.completion_status == "full_success"
        assert slip.processed_by == user

    def test_mark_partial_success(self) -> None:
        """FIN-BANK-UNIT-015: Partial success sets 'partially_processed'."""
        slip = PayingInSlipFactory(
            status="submitted_to_bank", total_amount=Decimal("500.00")
        )

        BankingService.mark_slip_as_processed(
            slip,
            processed_amount=Decimal("350.00"),
            completion_status="partial_success",
            bank_processed_date=datetime.date(2026, 2, 26),
        )
        slip.refresh_from_db()
        assert slip.status == "partially_processed"
        assert slip.completion_status == "partial_success"

    def test_mark_failed(self) -> None:
        """FIN-BANK-UNIT-016: Failed processing sets 'failed' status."""
        slip = PayingInSlipFactory(status="submitted_to_bank")

        BankingService.mark_slip_as_processed(
            slip,
            processed_amount=Decimal("0.00"),
            completion_status="failed",
            bank_processed_date=datetime.date(2026, 2, 26),
            processing_issues=["invalid_cheque"],
            custom_issue="Cheque bounced.",
        )
        slip.refresh_from_db()
        assert slip.status == "failed"
        assert slip.processing_issues == ["invalid_cheque"]
        assert slip.custom_issue == "Cheque bounced."

    def test_get_unprocessed_amount(self) -> None:
        """FIN-BANK-UNIT-017: Unprocessed amount = total - processed."""
        slip = PayingInSlipFactory(total_amount=Decimal("500.00"))
        slip.processed_amount = Decimal("350.00")
        assert slip.get_unprocessed_amount() == Decimal("150.00")

    def test_get_unprocessed_amount_none_processed(self) -> None:
        """FIN-BANK-UNIT-018: None processed returns full total."""
        slip = PayingInSlipFactory(total_amount=Decimal("500.00"))
        slip.processed_amount = None
        assert slip.get_unprocessed_amount() == Decimal("500.00")


# ═══════════════════════════════════════════════════════════════
# PayingInSlip — String Representation
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestSlipStr:
    """FIN-BANK-UNIT-019: __str__ format."""

    def test_str_includes_slip_number(self) -> None:
        """FIN-BANK-UNIT-019: __str__ includes slip number and client."""
        client = ClientFactory(name="TestClient")
        slip = PayingInSlipFactory(
            slip_number="TC-20260225-001",
            client=client,
            banking_date=datetime.date(2026, 2, 25),
        )
        s = str(slip)
        assert "TC-20260225-001" in s
        assert "TestClient" in s


# ═══════════════════════════════════════════════════════════════
# PayingInSlip — Add-Donations View Guard
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestSlipAddDonationsViewGuard:
    """FIN-BANK-UNIT-020: slip_add_donations rejects unsettled donations."""

    @staticmethod
    def _login_staff_client() -> Client:
        user = UserFactory(is_staff=True, is_superuser=True)
        user.set_password("testpass123!")
        user.save(update_fields=["password"])
        client = Client()
        assert client.login(username=user.username, password="testpass123!")
        return client

    def test_rejects_pending_card_donation(self) -> None:
        """FIN-BANK-UNIT-020: Adding a pending card donation returns 400."""
        client = self._login_staff_client()
        campaign = CampaignFactory(status="active")
        slip = PayingInSlipFactory(client=campaign.client)
        donation = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("100.00"),
            payment_method="card",
            payment_status="pending",
            qa_status="approved",
        )

        response = client.post(
            reverse("custom_admin:slip_add_donations", kwargs={"slip_id": slip.pk}),
            {"donation_ids": json.dumps([str(donation.id)])},
        )

        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["success"] is False
        assert "not settled" in payload["error"]
        # Offending donation IDs should be surfaced so the UI can highlight
        # them and the user has a recoverable error.
        assert "unsettled_donation_ids" in payload
        assert str(donation.id) in payload["unsettled_donation_ids"]
        donation.refresh_from_db()
        assert donation.paying_in_slip_id is None

    def test_accepts_completed_card_donation(self) -> None:
        """FIN-BANK-UNIT-021: Adding a completed card donation succeeds."""
        client = self._login_staff_client()
        campaign = CampaignFactory(status="active")
        slip = PayingInSlipFactory(client=campaign.client)
        donation = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("75.00"),
            payment_method="card",
            payment_status="completed",
            qa_status="approved",
        )

        response = client.post(
            reverse("custom_admin:slip_add_donations", kwargs={"slip_id": slip.pk}),
            {"donation_ids": json.dumps([str(donation.id)])},
        )

        assert response.status_code == 200
        payload = json.loads(response.content)
        assert payload["success"] is True
        assert payload["added_count"] == 1
        slip.refresh_from_db()
        assert slip.total_amount == Decimal("75.00")


# ═══════════════════════════════════════════════════════════════
# PayingInSlip — Detail / Print Display Filtering
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestSlipDisplayFiltersUnsettled:
    """FIN-BANK-UNIT-022 to 023: slip_detail/slip_print exclude unsettled rows.

    Pre-existing slips can carry pending-card donations that were attached
    before the recalculate filter went in. Those donations are excluded from
    ``slip.total_amount`` by ``recalculate_slip_totals`` but were still being
    iterated by the detail/print views, leaving the displayed sum mismatched
    against the header. The display views must filter through
    :data:`SLIP_SETTLED_DONATION_Q` so the rows on screen always sum to the
    stored total.
    """

    @staticmethod
    def _login_staff_client() -> Client:
        user = UserFactory(is_staff=True, is_superuser=True)
        user.set_password("testpass123!")
        user.save(update_fields=["password"])
        client = Client()
        assert client.login(username=user.username, password="testpass123!")
        return client

    def test_slip_detail_excludes_unsettled_donations_from_display(self) -> None:
        """FIN-BANK-UNIT-022: Pending-card donation hidden from slip_detail."""
        client = self._login_staff_client()
        campaign = CampaignFactory(status="active")
        slip = PayingInSlipFactory(client=campaign.client)
        settled = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("60.00"),
            payment_method="cheque",
            qa_status="approved",
            paying_in_slip=slip,
        )
        unsettled = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("999.00"),
            payment_method="card",
            payment_status="pending",
            qa_status="approved",
            paying_in_slip=slip,
        )

        response = client.get(
            reverse("custom_admin:slip_detail", kwargs={"slip_id": slip.pk})
        )

        assert response.status_code == 200
        donations = list(response.context["donations"])
        donation_ids = {d.id for d in donations}
        assert settled.id in donation_ids
        assert unsettled.id not in donation_ids

    def test_slip_print_excludes_unsettled_donations(self) -> None:
        """FIN-BANK-UNIT-023: Pending-card donation hidden from slip_print."""
        client = self._login_staff_client()
        campaign = CampaignFactory(status="active")
        slip = PayingInSlipFactory(client=campaign.client)
        settled = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("60.00"),
            payment_method="cheque",
            qa_status="approved",
            paying_in_slip=slip,
        )
        unsettled = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("999.00"),
            payment_method="card",
            payment_status="failed",
            qa_status="approved",
            paying_in_slip=slip,
        )

        response = client.get(
            reverse("custom_admin:slip_print", kwargs={"slip_id": slip.pk})
        )

        assert response.status_code == 200
        donations = list(response.context["donations"])
        donation_ids = {d.id for d in donations}
        assert settled.id in donation_ids
        assert unsettled.id not in donation_ids

        # Grouped output (used by the printed layout) must also not surface
        # the unsettled donation under any payment-method bucket.
        grouped = response.context["grouped_donations"]
        flattened_ids = {d.id for rows in grouped.values() for d in rows}
        assert unsettled.id not in flattened_ids
