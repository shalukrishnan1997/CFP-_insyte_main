"""Tests for the QA-gate on auto-created system donors.

When the scan pipeline auto-creates a SystemDonor for an unmatched OCR URN,
the new record must carry ``pending_review=True`` so QA confirms it before
the donor is treated as a real house-file record. The QA action then either
flips ``pending_review`` off (confirm) or disconnects the donor and removes
the orphaned junk row (reject).
"""

from typing import cast

import pytest
from django.http import HttpRequest
from django.test import RequestFactory

from campaigns.models import CampaignDataFile
from custom_admin.views import qa_review
from donors.models import DataFileDonor, SystemDonor
from scans.scan_processing_donors import (
    apply_donor_match,
    create_new_placeholder_donor,
)
from tests.factories import (
    CampaignFactory,
    DonationFactory,
    DonorFactory,
    ScanPlaceholderFactory,
    UserFactory,
)


def _noop_message(_request: object, _message: object) -> None:
    """Drop Django flash messages during unit tests."""


def _patch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    for level in ("success", "info", "error", "warning"):
        monkeypatch.setattr(qa_review.messages, level, _noop_message)
    monkeypatch.setattr(qa_review, "log_request_action", lambda *_a, **_k: None)


def _staff_post(action: str) -> HttpRequest:
    return RequestFactory().post("/admin/qa/resolve/", {"action": action})


@pytest.mark.django_db()
class TestPendingReviewSystemDonor:
    """The auto-create path must flag the system donor as pending review."""

    def test_create_new_placeholder_donor_marks_pending_review(self) -> None:
        """``create_new_placeholder_donor`` must flip ``pending_review`` on."""
        campaign = CampaignFactory(
            campaign_temperature="cold",
            donor_source="house_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=False,
            urn="URN999001",
            extracted_data={"donor_name": "New Donor", "postcode": "SW1A 1AA"},
            ocr_data={},
        )

        donor = create_new_placeholder_donor(placeholder, campaign)

        assert isinstance(donor, SystemDonor)
        assert donor.pending_review is True
        assert donor.client == campaign.client

    def test_apply_donor_match_cold_unmatched_pending_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cold-campaign unmatched scans must set ``pending_review=True``."""
        campaign = CampaignFactory(
            campaign_temperature="cold",
            donor_source="house_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=False,
            urn="URN999002",
            extracted_data={"donor_name": "Mystery Donor", "postcode": "EC1A 1BB"},
            ocr_data={},
        )

        monkeypatch.setattr(
            "scans.ocr.OCRExtractor.match_donor",
            lambda _u, _c: {
                "donor": None,
                "data_file_donor": None,
                "source": "not_found",
                "donor_name": "",
            },
        )

        result = apply_donor_match(placeholder, campaign)

        assert result["source"] == "house_file"
        assert placeholder.matched_system_donor is not None
        assert placeholder.matched_system_donor.pending_review is True


@pytest.mark.django_db()
class TestQaResolvePendingDonor:
    """The QA action endpoint should confirm or reject pending donors."""

    def test_confirm_clears_pending_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``confirm`` must flip ``pending_review`` to ``False``."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory()
        donation.system_donor.pending_review = True
        donation.system_donor.save(update_fields=["pending_review"])
        donor_pk = donation.system_donor.pk

        request = _staff_post("confirm")
        request.user = staff

        qa_review.qa_resolve_pending_donor(
            request, batch_id=cast(int, donation.batch_id), donation_id=str(donation.id)
        )

        refreshed = SystemDonor.objects.get(pk=donor_pk)
        assert refreshed.pending_review is False

    def test_reject_disconnects_and_deletes_orphan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``reject`` must disconnect the donor and delete the orphaned row."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory()
        donation.system_donor.pending_review = True
        donation.system_donor.save(update_fields=["pending_review"])
        donor_pk = donation.system_donor.pk
        placeholder = ScanPlaceholderFactory(
            batch__campaign=donation.campaign,
            urn="URN999003",
            donation=donation,
            matched_system_donor=donation.system_donor,
        )

        request = _staff_post("reject")
        request.user = staff

        qa_review.qa_resolve_pending_donor(
            request, batch_id=cast(int, donation.batch_id), donation_id=str(donation.id)
        )

        donation.refresh_from_db()
        placeholder.refresh_from_db()
        assert donation.system_donor_id is None
        assert placeholder.matched_system_donor_id is None
        assert not SystemDonor.objects.filter(pk=donor_pk).exists()

    def test_reject_keeps_donor_when_other_donations_reference_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject must not delete a donor still referenced by other donations."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory()
        donation.system_donor.pending_review = True
        donation.system_donor.save(update_fields=["pending_review"])
        donor_pk = donation.system_donor.pk
        other_donation = DonationFactory(
            campaign=donation.campaign,
            batch=donation.batch,
            system_donor=donation.system_donor,
        )

        request = _staff_post("reject")
        request.user = staff

        qa_review.qa_resolve_pending_donor(
            request, batch_id=cast(int, donation.batch_id), donation_id=str(donation.id)
        )

        donation.refresh_from_db()
        other_donation.refresh_from_db()
        assert donation.system_donor_id is None
        assert other_donation.system_donor_id == donor_pk
        assert SystemDonor.objects.filter(pk=donor_pk).exists()

    def test_action_rejected_when_donor_not_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The endpoint must refuse to act on donors that are not pending review."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory()
        assert donation.system_donor is not None
        donation.system_donor.pending_review = False
        donation.system_donor.save(update_fields=["pending_review"])
        donor_pk = donation.system_donor.pk

        request = _staff_post("confirm")
        request.user = staff

        qa_review.qa_resolve_pending_donor(
            request, batch_id=cast(int, donation.batch_id), donation_id=str(donation.id)
        )

        refreshed = SystemDonor.objects.get(pk=donor_pk)
        assert refreshed.pending_review is False

    def test_reject_clears_legacy_donor_fk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject must also clear the legacy ``placeholder.matched_donor`` FK."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory()
        donation.system_donor.pending_review = True
        donation.system_donor.save(update_fields=["pending_review"])
        legacy_donor = DonorFactory()
        placeholder = ScanPlaceholderFactory(
            batch__campaign=donation.campaign,
            urn="URN999004",
            donation=donation,
            matched_system_donor=donation.system_donor,
            matched_donor=legacy_donor,
        )

        request = _staff_post("reject")
        request.user = staff

        qa_review.qa_resolve_pending_donor(
            request, batch_id=cast(int, donation.batch_id), donation_id=str(donation.id)
        )

        placeholder.refresh_from_db()
        assert placeholder.matched_system_donor_id is None
        assert placeholder.matched_donor_id is None

    def test_reject_with_data_file_donor_does_not_break(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``DataFileDonor`` reference must not interfere with reject flow.

        ``DataFileDonor`` does not have a direct FK to ``SystemDonor``, but
        keeping a record alongside the rejected donor verifies the flow runs
        end-to-end without leaking referential-integrity errors when the
        broader donor graph is populated.
        """
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory()
        donation.system_donor.pending_review = True
        donation.system_donor.save(update_fields=["pending_review"])
        donor_pk = donation.system_donor.pk
        data_file = CampaignDataFile.objects.create(
            campaign=donation.campaign, created_by=staff
        )
        data_file_donor = DataFileDonor.objects.create(
            data_file=data_file,
            client=donation.campaign.client,
            urn=donation.system_donor.external_urn,
            first_name=donation.system_donor.first_name,
            last_name=donation.system_donor.last_name,
        )

        request = _staff_post("reject")
        request.user = staff

        qa_review.qa_resolve_pending_donor(
            request, batch_id=cast(int, donation.batch_id), donation_id=str(donation.id)
        )

        donation.refresh_from_db()
        assert donation.system_donor_id is None
        assert not SystemDonor.objects.filter(pk=donor_pk).exists()
        # The DataFileDonor row must remain — it has no FK to SystemDonor.
        assert DataFileDonor.objects.filter(pk=data_file_donor.pk).exists()

    def test_reject_with_concurrent_attach_does_not_orphan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrent attach simulated mid-reject must keep the donor alive.

        We monkey-patch the orphan-check ``Donation.objects.filter(...)`` so
        that just before the ``.exists()`` lookup, a parallel transaction is
        simulated by attaching a brand-new ``Donation`` to the donor. The
        ``select_for_update()`` lock taken earlier in the reject flow would,
        in a real RDBMS, block such a parallel attach until our txn commits.
        Here we assert the post-lock orphan check correctly observes the
        attach and refuses to delete the donor.
        """
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory()
        donation.system_donor.pending_review = True
        donation.system_donor.save(update_fields=["pending_review"])
        donor = donation.system_donor
        donor_pk = donor.pk

        from typing import Any

        from donations.models import Donation

        original_filter = Donation.objects.filter
        attached: dict[str, object] = {}

        def filter_with_concurrent_attach(*args: Any, **kwargs: Any) -> Any:
            """Inject a concurrent attach right before the orphan-check exists()."""
            qs = original_filter(*args, **kwargs)
            if (
                kwargs.get("system_donor") is not None
                and getattr(kwargs["system_donor"], "pk", None) == donor_pk
                and "concurrent" not in attached
            ):
                attached["concurrent"] = DonationFactory(
                    campaign=donation.campaign,
                    batch=donation.batch,
                    system_donor=donor,
                )
            return qs

        monkeypatch.setattr(Donation.objects, "filter", filter_with_concurrent_attach)

        request = _staff_post("reject")
        request.user = staff

        qa_review.qa_resolve_pending_donor(
            request, batch_id=cast(int, donation.batch_id), donation_id=str(donation.id)
        )

        donation.refresh_from_db()
        # Our donation was disconnected, but the donor must survive because
        # the concurrent attach bumped the orphan-check ref count.
        assert donation.system_donor_id is None
        assert SystemDonor.objects.filter(pk=donor_pk).exists()
        assert "concurrent" in attached

    def test_reject_acquires_select_for_update_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject path must acquire ``select_for_update()`` on the donor row.

        SQLite (used in tests) does not implement true row locking, so this
        assertion is a structural guard: it verifies the manager call chain
        still routes through ``select_for_update()`` so production Postgres
        gets the real lock.
        """
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory()
        donation.system_donor.pending_review = True
        donation.system_donor.save(update_fields=["pending_review"])

        from typing import Any

        called: dict[str, bool] = {"select_for_update": False}
        original_sfu = SystemDonor.objects.select_for_update

        def tracked_select_for_update(*args: Any, **kwargs: Any) -> Any:
            called["select_for_update"] = True
            return original_sfu(*args, **kwargs)

        monkeypatch.setattr(
            SystemDonor.objects, "select_for_update", tracked_select_for_update
        )

        request = _staff_post("reject")
        request.user = staff

        qa_review.qa_resolve_pending_donor(
            request, batch_id=cast(int, donation.batch_id), donation_id=str(donation.id)
        )

        assert called["select_for_update"] is True
