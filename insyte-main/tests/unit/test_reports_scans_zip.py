"""Tests for the per-report 'Download scans (ZIP)' export.

Covers the staff-only access guard, the QA-approved + redaction-completed
filter, the legacy ``image_path`` fallback, the empty-result README
contract, and the unsupported-report-type 400 path.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from django.test import RequestFactory

from custom_admin.views.reports.scans_zip import (
    SCAN_ZIP_UNSUPPORTED_REPORTS,
    report_scans_zip,
    report_supports_scan_zip,
)
from donations.models import Donation
from scans.models import ScanPlaceholder
from tests.factories import (
    CampaignFactory,
    DonationFactory,
    DonorFactory,
    ScanPlaceholderFactory,
    UserFactory,
)


def _staff_user() -> Any:
    """Staff user that passes the admin permission decorator."""
    return UserFactory(is_staff=True, is_superuser=False)


def _approved_donation_with_scan(
    *,
    campaign: Any,
    donor: Any,
    page_keys: list[str] | None = None,
    image_path: str = "ScanOutput/test/scan.tiff",
    redaction_status: str = ScanPlaceholder.REDACTION_COMPLETED,
) -> tuple[Any, ScanPlaceholder]:
    """Build a QA-approved Donation linked to a redacted ScanPlaceholder."""
    donation = DonationFactory(
        campaign=campaign,
        donor=donor,
        amount=Decimal("25.00"),
        donation_date=date(2026, 3, 20),
        qa_status=Donation.QA_STATUS_APPROVED,
    )
    placeholder = ScanPlaceholderFactory(
        donation=donation,
        page_keys=page_keys or [],
        image_path=image_path,
        redaction_status=redaction_status,
    )
    return donation, placeholder


@pytest.mark.django_db()
class TestReportSupportsScanZip:
    """Pure-function check used by the view + template."""

    def test_donation_grain_reports_are_supported(self) -> None:
        for report_type in (
            "donations",
            "banking",
            "gift_aid",
            "hgv",
            "lgv",
            "unmatched_donors",
        ):
            assert report_supports_scan_zip(report_type) is True, report_type

    def test_aggregate_reports_are_unsupported(self) -> None:
        for report_type in SCAN_ZIP_UNSUPPORTED_REPORTS:
            assert report_supports_scan_zip(report_type) is False, report_type

    def test_unknown_report_type_is_unsupported(self) -> None:
        assert report_supports_scan_zip("bogus") is False


@pytest.mark.django_db()
class TestReportScansZipAccessControl:
    """Anonymous users are bounced; non-staff are bounced; staff get a ZIP."""

    def test_anonymous_user_is_redirected_to_login(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {"report_type": "donations"},
        )
        request.user = AnonymousUser()
        # Decorator pulls messages framework — install a no-op store.
        request.session = {}  # type: ignore[attr-defined]
        request._messages = _DummyMessages()  # type: ignore[attr-defined]

        response = report_scans_zip(request)

        # Decorator returns a redirect to login.
        assert response.status_code == 302

    def test_non_staff_no_perms_user_is_blocked(self) -> None:
        user = UserFactory(is_staff=False, is_superuser=False)
        # Strip the auto-applied permissions so user_has_access returns False.
        user.user_permissions.clear()
        user.groups.clear()

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {"report_type": "donations"},
        )
        request.user = user
        request.session = {}  # type: ignore[attr-defined]
        request._messages = _DummyMessages()  # type: ignore[attr-defined]

        response = report_scans_zip(request)

        # is_authenticated_and_is_staff redirects to auth_app:login (302).
        assert response.status_code == 302

    def test_staff_user_gets_zip_response(self) -> None:
        staff = _staff_user()
        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {"report_type": "donations"},
        )
        request.user = staff

        with patch(
            "scans.scan_processing_r2.build_pdf_bytes_from_r2_keys",
            return_value=_minimal_pdf_bytes(),
        ):
            response = report_scans_zip(request)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/zip"
        assert response["Content-Disposition"].startswith("attachment;")
        assert response["Content-Disposition"].endswith('.zip"')


@pytest.mark.django_db()
class TestReportScansZipUnsupportedReport:
    """Aggregate reports return 400 instead of an empty ZIP."""

    @pytest.mark.parametrize("report_type", sorted(SCAN_ZIP_UNSUPPORTED_REPORTS))
    def test_aggregate_report_returns_400(self, report_type: str) -> None:
        staff = _staff_user()
        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {"report_type": report_type},
        )
        request.user = staff

        response = report_scans_zip(request)

        assert response.status_code == 400


@pytest.mark.django_db()
class TestReportScansZipHappyPath:
    """ZIP contains the QA-approved, redacted scans for the date range."""

    def test_zip_contains_pdfs_for_matched_donations(self) -> None:
        staff = _staff_user()
        campaign = CampaignFactory()
        donor_a = DonorFactory(client=campaign.client)
        donor_b = DonorFactory(client=campaign.client)

        donation_a, placeholder_a = _approved_donation_with_scan(
            campaign=campaign,
            donor=donor_a,
            page_keys=["ScanOutput/2026/03/donor_a.pdf"],
        )
        donation_b, placeholder_b = _approved_donation_with_scan(
            campaign=campaign,
            donor=donor_b,
            page_keys=[
                "ScanOutput/2026/03/donor_b_p1.pdf",
                "ScanOutput/2026/03/donor_b_p2.pdf",
            ],
        )

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {
                "report_type": "donations",
                "date_from": "01/03/2026",
                "date_to": "31/03/2026",
            },
        )
        request.user = staff

        with patch(
            "scans.scan_processing_r2.build_pdf_bytes_from_r2_keys",
            return_value=_minimal_pdf_bytes(),
        ) as mocked:
            response = report_scans_zip(request)

        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = sorted(archive.namelist())
        assert names == sorted(
            [
                f"{donation_a.id}_{placeholder_a.urn}_scan.pdf",
                f"{donation_b.id}_{placeholder_b.urn}_scan.pdf",
            ]
        )
        # Each donation maps to one zip entry, regardless of how many R2 page
        # keys back the placeholder.
        assert mocked.call_count == 2
        assert "README.txt" not in names

    def test_zip_uses_image_path_fallback_when_page_keys_empty(self) -> None:
        staff = _staff_user()
        campaign = CampaignFactory()
        donor = DonorFactory(client=campaign.client)
        donation, placeholder = _approved_donation_with_scan(
            campaign=campaign,
            donor=donor,
            page_keys=[],
            image_path="ScanOutput/2026/03/legacy_only.tiff",
        )

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {
                "report_type": "donations",
                "date_from": "01/03/2026",
                "date_to": "31/03/2026",
            },
        )
        request.user = staff

        with patch(
            "scans.scan_processing_r2.build_pdf_bytes_from_r2_keys",
            return_value=_minimal_pdf_bytes(),
        ) as mocked:
            response = report_scans_zip(request)

        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert archive.namelist() == [
            f"{donation.id}_{placeholder.urn}_scan.pdf",
        ]
        # Builder was called with the legacy single-key list.
        mocked.assert_called_once_with([placeholder.image_path])


@pytest.mark.django_db()
class TestReportScansZipFiltering:
    """Donations missing the gating signals are silently skipped."""

    def test_donation_without_scan_placeholder_is_skipped(self) -> None:
        staff = _staff_user()
        campaign = CampaignFactory()
        donor = DonorFactory(client=campaign.client)
        # No placeholder linked.
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("12.00"),
            donation_date=date(2026, 3, 15),
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {
                "report_type": "donations",
                "date_from": "01/03/2026",
                "date_to": "31/03/2026",
            },
        )
        request.user = staff

        with patch(
            "scans.scan_processing_r2.build_pdf_bytes_from_r2_keys",
            return_value=_minimal_pdf_bytes(),
        ) as mocked:
            response = report_scans_zip(request)

        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        # No PDFs, README placeholder.
        assert archive.namelist() == ["README.txt"]
        assert mocked.call_count == 0

    def test_donation_pending_qa_is_skipped(self) -> None:
        staff = _staff_user()
        campaign = CampaignFactory()
        donor = DonorFactory(client=campaign.client)
        donation = DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("18.00"),
            donation_date=date(2026, 3, 15),
            qa_status=Donation.QA_STATUS_PENDING,
        )
        ScanPlaceholderFactory(
            donation=donation,
            page_keys=["ScanOutput/2026/03/pending_qa.pdf"],
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
        )

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {
                "report_type": "donations",
                "date_from": "01/03/2026",
                "date_to": "31/03/2026",
            },
        )
        request.user = staff

        with patch(
            "scans.scan_processing_r2.build_pdf_bytes_from_r2_keys",
            return_value=_minimal_pdf_bytes(),
        ) as mocked:
            response = report_scans_zip(request)

        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert archive.namelist() == ["README.txt"]
        assert mocked.call_count == 0

    def test_donation_with_unredacted_scan_is_skipped(self) -> None:
        staff = _staff_user()
        campaign = CampaignFactory()
        donor = DonorFactory(client=campaign.client)
        donation = DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("18.00"),
            donation_date=date(2026, 3, 15),
            qa_status=Donation.QA_STATUS_APPROVED,
        )
        ScanPlaceholderFactory(
            donation=donation,
            page_keys=["ScanOutput/2026/03/unredacted.pdf"],
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {
                "report_type": "donations",
                "date_from": "01/03/2026",
                "date_to": "31/03/2026",
            },
        )
        request.user = staff

        with patch(
            "scans.scan_processing_r2.build_pdf_bytes_from_r2_keys",
            return_value=_minimal_pdf_bytes(),
        ) as mocked:
            response = report_scans_zip(request)

        archive = zipfile.ZipFile(io.BytesIO(response.content))
        # Unredacted PCI/PII content must never reach the export bundle.
        assert archive.namelist() == ["README.txt"]
        assert mocked.call_count == 0


@pytest.mark.django_db()
class TestReportScansZipEmptyResult:
    """Empty filter results return a valid ZIP with a README."""

    def test_empty_result_returns_valid_zip_with_readme(self) -> None:
        staff = _staff_user()
        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {
                "report_type": "donations",
                "date_from": "01/03/2026",
                "date_to": "31/03/2026",
            },
        )
        request.user = staff

        response = report_scans_zip(request)

        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = archive.namelist()
        assert names == ["README.txt"]
        readme = archive.read("README.txt").decode("utf-8")
        assert "No scans matched" in readme


@pytest.mark.django_db()
class TestReportScansZipPdfBuildFailure:
    """A failed PDF assembly is logged and skipped, not surfaced as 500."""

    def test_pdf_build_failure_is_skipped_silently(self) -> None:
        staff = _staff_user()
        campaign = CampaignFactory()
        donor_ok = DonorFactory(client=campaign.client)
        donor_fail = DonorFactory(client=campaign.client)
        donation_ok, placeholder_ok = _approved_donation_with_scan(
            campaign=campaign,
            donor=donor_ok,
            page_keys=["ScanOutput/2026/03/ok.pdf"],
        )
        _approved_donation_with_scan(
            campaign=campaign,
            donor=donor_fail,
            page_keys=["ScanOutput/2026/03/explodes.pdf"],
        )

        def fake_builder(page_keys: list[str]) -> bytes:
            if any("explodes" in key for key in page_keys):
                raise RuntimeError("R2 not configured in test")
            return _minimal_pdf_bytes()

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {
                "report_type": "donations",
                "date_from": "01/03/2026",
                "date_to": "31/03/2026",
            },
        )
        request.user = staff

        with patch(
            "scans.scan_processing_r2.build_pdf_bytes_from_r2_keys",
            side_effect=fake_builder,
        ):
            response = report_scans_zip(request)

        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        # Only the donor whose builder succeeded is in the bundle.
        assert archive.namelist() == [
            f"{donation_ok.id}_{placeholder_ok.urn}_scan.pdf",
        ]


@pytest.mark.django_db()
class TestReportScansZipTruncation:
    """When matched scans exceed ``MAX_SCANS_PER_ZIP`` the bundle truncates."""

    def test_truncation_caps_archive_and_emits_readme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch the cap to keep the test fast — same code path as production,
        # without having to fabricate 500+ donations.
        monkeypatch.setattr("custom_admin.views.reports.scans_zip.MAX_SCANS_PER_ZIP", 2)

        staff = _staff_user()
        campaign = CampaignFactory()
        donations: list[tuple[Any, ScanPlaceholder]] = []
        for index in range(3):
            donor = DonorFactory(client=campaign.client)
            donations.append(
                _approved_donation_with_scan(
                    campaign=campaign,
                    donor=donor,
                    page_keys=[f"ScanOutput/2026/03/donor_{index}.pdf"],
                )
            )

        request = RequestFactory().get(
            "/admin/reports/scans-zip/",
            {
                "report_type": "donations",
                "date_from": "01/03/2026",
                "date_to": "31/03/2026",
            },
        )
        request.user = staff

        with patch(
            "scans.scan_processing_r2.build_pdf_bytes_from_r2_keys",
            return_value=_minimal_pdf_bytes(),
        ):
            response = report_scans_zip(request)

        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = archive.namelist()
        # 2 PDFs + a truncation README, never the third PDF.
        pdf_entries = [name for name in names if name.endswith("_scan.pdf")]
        assert len(pdf_entries) == 2
        assert "README.txt" in names
        readme = archive.read("README.txt").decode("utf-8")
        assert "Result truncated" in readme
        assert "capped at 2 entries" in readme


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _minimal_pdf_bytes() -> bytes:
    """Return a tiny but well-formed PDF byte string for archive payloads."""
    return b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class _DummyMessages:
    """Stand-in for the messages framework when bypassing middleware."""

    def add(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def update(self, *_args: Any, **_kwargs: Any) -> None:
        return None
