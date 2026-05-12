"""Regression tests for admin-side report views and generators.

Covers behavior the client-portal tests don't exercise:
- Admin-side permission and cross-client scope (``scope=overall``).
- HGV/LGV SQL-pushdown filters (threshold equality, fallback defaults,
  campaign-less donations).
- Empty-state: ``has_visualization`` is False when there are no rows.
- N+1 prevention: donor/campaign chains are pre-fetched.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from custom_admin.views.reports.generators import (
    _generate_donations_report,
    _generate_hgv_report,
    _generate_lgv_report,
    _generate_payment_method_report,
    _generate_unmatched_donors_report,
)
from custom_admin.views.reports.helpers import _get_filtered_donations
from custom_admin.views.reports.htmx import (
    admin_reports_filter_partial,
    admin_reports_results_partial,
)
from custom_admin.views.reports.main import admin_reports, report_export
from donations.models import Donation
from donors.models import Donor, SystemDonor
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationFactory,
    DonorFactory,
    SystemDonorFactory,
    UserFactory,
)


def _staff_user() -> Any:
    """Staff user that passes the admin permission decorator."""
    return UserFactory(is_staff=True, is_superuser=False)


@pytest.mark.django_db()
class TestAdminReportHgvLgv:
    """HGV/LGV now push thresholds into SQL — lock the edge cases."""

    def test_hgv_includes_donations_at_or_above_threshold(self) -> None:
        campaign = CampaignFactory(hgv_amount=Decimal("500.00"))
        donor = DonorFactory(client=campaign.client)
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("500.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("499.99"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("1500.00"),
            donation_date=date(2026, 3, 20),
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_hgv_report(donations)

        amounts = sorted(row[4] for row in rows)
        assert amounts == ["£1500.00", "£500.00"]

    def test_hgv_falls_back_to_default_when_campaign_threshold_zero(self) -> None:
        campaign = CampaignFactory(hgv_amount=Decimal("0"))
        donor = DonorFactory(client=campaign.client)
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("1000.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("999.99"),
            donation_date=date(2026, 3, 20),
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_hgv_report(donations)

        assert len(rows) == 1
        assert rows[0][4] == "£1000.00"
        assert rows[0][5] == "£1000.00"

    def test_lgv_includes_donations_at_or_below_threshold(self) -> None:
        campaign = CampaignFactory(lgv_amount=Decimal("50.00"))
        donor = DonorFactory(client=campaign.client)
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("50.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("50.01"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("5.00"),
            donation_date=date(2026, 3, 20),
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_lgv_report(donations)

        amounts = sorted(row[4] for row in rows)
        assert amounts == ["£5.00", "£50.00"]

    def test_lgv_falls_back_to_default_when_campaign_threshold_zero(self) -> None:
        campaign = CampaignFactory(lgv_amount=Decimal("0"))
        donor = DonorFactory(client=campaign.client)
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("50.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("50.01"),
            donation_date=date(2026, 3, 20),
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_lgv_report(donations)

        assert len(rows) == 1
        assert rows[0][4] == "£50.00"
        assert rows[0][5] == "£50.00"


@pytest.mark.django_db()
class TestAdminReportViews:
    """Permission, cross-client scope, and empty-state behavior."""

    def test_admin_reports_renders_landing_page_for_staff(self) -> None:
        staff = _staff_user()
        ClientFactory(name="Alpha")
        ClientFactory(name="Beta")

        request = RequestFactory().get("/admin/reports/")
        request.user = staff
        request.session = {}  # type: ignore[attr-defined]

        response = admin_reports(request)

        assert response.status_code == 200

    def test_filter_partial_lists_all_active_clients(self) -> None:
        staff = _staff_user()
        visible = ClientFactory(name="Visible", is_active=True)
        other = ClientFactory(name="Also Visible", is_active=True)
        ClientFactory(name="Inactive", is_active=False)

        request = RequestFactory().get("/admin/reports/htmx/filters/donations/")
        request.user = staff

        response = admin_reports_filter_partial(request, report_type="donations")

        assert response.status_code == 200
        body = response.content.decode()
        assert visible.name in body
        assert other.name in body
        assert "Inactive" not in body

    def test_filter_partial_rejects_unknown_report_type(self) -> None:
        staff = _staff_user()
        request = RequestFactory().get("/admin/reports/htmx/filters/bogus/")
        request.user = staff

        response = admin_reports_filter_partial(request, report_type="bogus")

        assert response.status_code == 400

    def test_results_partial_scope_overall_aggregates_across_clients(self) -> None:
        staff = _staff_user()
        client_a = ClientFactory(name="A")
        client_b = ClientFactory(name="B")
        campaign_a = CampaignFactory(client=client_a)
        campaign_b = CampaignFactory(client=client_b)
        donor_a = DonorFactory(client=client_a)
        donor_b = DonorFactory(client=client_b)
        DonationFactory(
            campaign=campaign_a,
            donor=donor_a,
            amount=Decimal("10.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign_b,
            donor=donor_b,
            amount=Decimal("20.00"),
            donation_date=date(2026, 3, 20),
        )

        request = RequestFactory().post(
            "/admin/reports/htmx/results/",
            {
                "report_type": "donations",
                "scope": "overall",
                "date_from": "2026-03-01",
                "date_to": "2026-03-31",
            },
        )
        request.user = staff

        response = admin_reports_results_partial(request)

        assert response.status_code == 200
        body = response.content.decode()
        # Both donations (from different clients) appear under scope=overall.
        assert "£10.00" in body
        assert "£20.00" in body

    def test_results_partial_rejects_invalid_report_type(self) -> None:
        staff = _staff_user()
        request = RequestFactory().post(
            "/admin/reports/htmx/results/",
            {"report_type": "bogus"},
        )
        request.user = staff

        response = admin_reports_results_partial(request)

        assert response.status_code == 400

    def test_results_partial_hides_chart_when_no_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty results should suppress the chart card (has_visualization=False)."""
        staff = _staff_user()
        captured: dict[str, Any] = {}

        def fake_render(_request: Any, _template: str, context: dict[str, Any]) -> Any:
            captured.update(context)
            from django.http import HttpResponse

            return HttpResponse("ok")

        monkeypatch.setattr("custom_admin.views.reports.htmx.render", fake_render)

        request = RequestFactory().post(
            "/admin/reports/htmx/results/",
            {
                "report_type": "donations",
                "scope": "overall",
                "date_from": "2026-03-01",
                "date_to": "2026-03-31",
            },
        )
        request.user = staff

        response = admin_reports_results_partial(request)

        assert response.status_code == 200
        assert captured["total_records"] == 0
        assert captured["has_visualization"] is False
        assert captured["chart_data_json"] == "null"

    def test_csv_export_respects_client_filter(self) -> None:
        staff = _staff_user()
        client_a = ClientFactory(name="ClientA")
        client_b = ClientFactory(name="ClientB")
        campaign_a = CampaignFactory(client=client_a, name="Alpha Appeal")
        campaign_b = CampaignFactory(client=client_b, name="Beta Appeal")
        donor_a = DonorFactory(client=client_a, first_name="Alice", last_name="A")
        donor_b = DonorFactory(client=client_b, first_name="Bob", last_name="B")
        DonationFactory(
            campaign=campaign_a,
            donor=donor_a,
            amount=Decimal("11.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign_b,
            donor=donor_b,
            amount=Decimal("99.00"),
            donation_date=date(2026, 3, 20),
        )

        request = RequestFactory().get(
            "/admin/reports/export/",
            {
                "report_type": "donations",
                "format": "csv",
                "date_from": "20/03/2026",
                "date_to": "20/03/2026",
                "client": str(client_a.id),
            },
        )
        request.user = staff

        response = report_export(request)
        body = response.content.decode()

        assert response.status_code == 200
        assert "Alice A" in body
        assert "Bob B" not in body
        assert "11.00" in body
        assert "99.00" not in body


@pytest.mark.django_db()
class TestReportQueryCount:
    """Guard against N+1 regressions in the donor/campaign chain."""

    def test_donations_report_query_count_does_not_scale_with_rows(self) -> None:
        campaign = CampaignFactory(name="Perf Appeal")
        # 10 donations with distinct donors — each row touches donor + campaign.
        for _ in range(10):
            donor = DonorFactory(client=campaign.client)
            DonationFactory(
                campaign=campaign,
                donor=donor,
                amount=Decimal("10.00"),
                donation_date=date(2026, 3, 20),
            )

        from custom_admin.views.reports.generators import _generate_donations_report

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        with CaptureQueriesContext(connection) as ctx:
            rows = _generate_donations_report(donations)

        assert len(rows) == 10
        # select_related keeps the generator to a single query for 10 rows.
        # Allow a small margin for session/auth but fail if it scales with N.
        assert len(ctx.captured_queries) <= 3

    def test_unmatched_donors_report_query_count_does_not_scale_with_rows(
        self,
    ) -> None:
        """Pin the prefetch_related contract on the SystemDonor branch.

        The new union over Donor + SystemDonor depends on
        ``prefetch_related("donations__campaign")`` to avoid an N+1 on
        SystemDonor → donations → campaign. Without the prefetch, each
        SystemDonor would fire its own query for ``sysdonor.donations``
        and again per donation for ``d.campaign``.
        """
        client = ClientFactory()
        campaign = CampaignFactory(client=client, name="Perf Appeal")
        for _ in range(10):
            sys_donor = SystemDonorFactory(client=client, pending_review=True)
            DonationFactory(
                campaign=campaign,
                donor=None,
                system_donor=sys_donor,
                donor_source="house_file",
            )

        with CaptureQueriesContext(connection) as ctx:
            rows = _generate_unmatched_donors_report(client_id=str(client.id))

        assert len(rows) == 10
        # Donor queryset (1) + SystemDonor queryset (1) +
        # prefetch for scan_placeholders__batch__campaign (~3 levels) +
        # prefetch for donations__campaign (~2 levels). Allow headroom
        # but fail loudly if the N+1 returns.
        assert len(ctx.captured_queries) <= 8


@pytest.mark.django_db()
class TestPhoneDonationsInReports:
    """Phone-intake donations must appear in the main report generators.

    Reports filter on date / client / campaign only — there is no
    ``intake_method`` filter (see ``_get_filtered_donations``). These
    tests pin that contract so a future filter tweak can't silently
    drop phone donations from operator-facing reports.
    """

    def test_phone_donation_appears_in_donations_report(self) -> None:
        campaign = CampaignFactory()
        donor = DonorFactory(client=campaign.client)
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("50.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status=Donation.QA_STATUS_APPROVED,
            donation_date=date(2026, 3, 20),
            field_data={"intake_method": "phone"},
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_donations_report(donations)

        assert len(rows) == 1
        assert rows[0][4] == "£50.00"

    def test_phone_donation_counted_in_payment_method_breakdown(self) -> None:
        campaign = CampaignFactory()
        donor = DonorFactory(client=campaign.client)
        # Scan-style card donation.
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("30.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            donation_date=date(2026, 3, 20),
        )
        # Phone-intake card donation.
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("70.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            donation_date=date(2026, 3, 20),
            field_data={"intake_method": "phone"},
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_payment_method_report(donations)

        # One bucket: "Credit/Debit Card" — the breakdown groups by
        # payment_method, not intake_method, so both donations roll up
        # into a single £100.00 row.
        assert len(rows) == 1
        assert rows[0][0] == "Credit/Debit Card"
        assert rows[0][1] == "2"
        assert rows[0][2] == "£100.00"

    def test_phone_donation_above_hgv_threshold(self) -> None:
        campaign = CampaignFactory(hgv_amount=Decimal("500.00"))
        donor = DonorFactory(client=campaign.client)
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("1500.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            donation_date=date(2026, 3, 20),
            field_data={"intake_method": "phone"},
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_hgv_report(donations)

        assert len(rows) == 1
        assert rows[0][4] == "£1500.00"

    def test_phone_donation_below_lgv_threshold(self) -> None:
        campaign = CampaignFactory(lgv_amount=Decimal("50.00"))
        donor = DonorFactory(client=campaign.client)
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("25.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            donation_date=date(2026, 3, 20),
            field_data={"intake_method": "phone"},
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_lgv_report(donations)

        assert len(rows) == 1
        assert rows[0][4] == "£25.00"

    def test_end_to_end_phone_intake_donation_in_donations_report(self) -> None:
        """End-to-end: drive the actual phone-intake helpers, then assert
        the resulting Donation appears in ``_generate_donations_report``.

        Unlike the field_data regression pins above, this exercises
        ``create_phone_intake_batch`` + ``create_phone_donation`` so the
        full intake code path is covered by at least one report-side test.
        """
        from donations.intake import create_phone_donation, create_phone_intake_batch

        operator = UserFactory()
        campaign = CampaignFactory()
        sys_donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=sys_donor,
            payload={
                "payment_method": Donation.PAYMENT_METHOD_CHEQUE,
                "amount": Decimal("123.45"),
                "currency": "GBP",
                "donation_date": date(2026, 3, 20),
                "gift_aid": False,
                "cheque_number": "100100",
                "cheque_date": date(2026, 3, 20),
            },
        )

        donations = _get_filtered_donations(date(2026, 3, 1), date(2026, 3, 31), "", "")
        rows = _generate_donations_report(donations)

        assert len(rows) == 1
        assert rows[0][4] == "£123.45"


@pytest.mark.django_db()
class TestUnmatchedDonorsReport:
    """The Unmatched Donors report unions two review queues:

    - ``Donor.verification_status=PENDING_EXPORT`` (created during admin
      donation entry on house-file campaigns when ``is_new_donor=True``).
    - ``SystemDonor.pending_review=True`` (created by scan OCR auto-match
      and by phone-intake inline donor creation).

    These tests pin both intake paths and the merge invariants.
    """

    def test_includes_pending_export_donor(self) -> None:
        client = ClientFactory()
        DonorFactory(
            client=client,
            verification_status=Donor.VERIFICATION_PENDING_EXPORT,
            first_name="Pending",
            last_name="Export",
        )

        rows = _generate_unmatched_donors_report(client_id=str(client.id))

        assert len(rows) == 1
        assert rows[0][1] == "Pending"
        assert rows[0][2] == "Export"

    def test_includes_pending_review_system_donor(self) -> None:
        client = ClientFactory()
        SystemDonorFactory(
            client=client,
            pending_review=True,
            first_name="Phone",
            last_name="Intake",
        )

        rows = _generate_unmatched_donors_report(client_id=str(client.id))

        assert len(rows) == 1
        assert rows[0][1] == "Phone"
        assert rows[0][2] == "Intake"

    def test_excludes_system_donors_without_pending_review(self) -> None:
        client = ClientFactory()
        SystemDonorFactory(
            client=client,
            pending_review=False,
            first_name="Already",
            last_name="Verified",
        )

        rows = _generate_unmatched_donors_report(client_id=str(client.id))

        assert rows == []

    def test_excludes_verified_donors(self) -> None:
        client = ClientFactory()
        DonorFactory(
            client=client,
            verification_status=Donor.VERIFICATION_VERIFIED,
            first_name="Already",
            last_name="Verified",
        )

        rows = _generate_unmatched_donors_report(client_id=str(client.id))

        assert rows == []

    def test_filters_system_donors_by_client(self) -> None:
        client_a = ClientFactory()
        client_b = ClientFactory()
        SystemDonorFactory(
            client=client_a,
            pending_review=True,
            first_name="Mine",
            last_name="A",
        )
        SystemDonorFactory(
            client=client_b,
            pending_review=True,
            first_name="Theirs",
            last_name="B",
        )

        rows = _generate_unmatched_donors_report(client_id=str(client_a.id))

        assert len(rows) == 1
        assert rows[0][1] == "Mine"

    def test_filters_system_donors_by_date_range(self) -> None:
        client = ClientFactory()
        in_range = SystemDonorFactory(
            client=client,
            pending_review=True,
            first_name="In",
            last_name="Range",
        )
        out_of_range = SystemDonorFactory(
            client=client,
            pending_review=True,
            first_name="Out",
            last_name="OfRange",
        )
        # Both rows start with auto_now_add today; .update() bypasses
        # auto_now_add so we can pin one inside the filter window and
        # one outside it. Without the explicit updates, both rows'
        # creation timestamps depend on test-run date and the assertion
        # would silently drift to a no-op.
        SystemDonor.objects.filter(pk=in_range.pk).update(
            created_at=timezone.make_aware(datetime(2026, 3, 15, 12, 0, 0)),
        )
        SystemDonor.objects.filter(pk=out_of_range.pk).update(
            created_at=timezone.make_aware(datetime(2026, 1, 1, 12, 0, 0)),
        )

        rows = _generate_unmatched_donors_report(
            client_id=str(client.id),
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )

        assert len(rows) == 1
        assert rows[0][1] == "In"

    def test_derives_campaign_for_system_donor_via_donations(self) -> None:
        client = ClientFactory()
        campaign = CampaignFactory(client=client, name="Spring Appeal")
        sys_donor = SystemDonorFactory(
            client=client,
            pending_review=True,
            first_name="Phone",
            last_name="Donor",
        )
        DonationFactory(
            campaign=campaign,
            donor=None,
            system_donor=sys_donor,
            donor_source="house_file",
        )

        rows = _generate_unmatched_donors_report(client_id=str(client.id))

        assert len(rows) == 1
        # Campaign column is the last cell.
        assert rows[0][-1] == "Spring Appeal"

    def test_orders_by_created_at_across_models(self) -> None:
        client = ClientFactory()
        first = DonorFactory(
            client=client,
            verification_status=Donor.VERIFICATION_PENDING_EXPORT,
            first_name="First",
            last_name="Created",
        )
        second = SystemDonorFactory(
            client=client,
            pending_review=True,
            first_name="Second",
            last_name="Created",
        )
        Donor.objects.filter(pk=first.pk).update(
            created_at=timezone.make_aware(datetime(2026, 3, 1, 9, 0, 0)),
        )
        SystemDonor.objects.filter(pk=second.pk).update(
            created_at=timezone.make_aware(datetime(2026, 3, 5, 9, 0, 0)),
        )

        rows = _generate_unmatched_donors_report(client_id=str(client.id))

        assert [row[1] for row in rows] == ["First", "Second"]
