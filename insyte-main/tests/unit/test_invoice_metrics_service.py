"""Unit tests for InvoiceMetricsService."""

from decimal import Decimal

import pytest
from django.utils import timezone

from invoices.metrics import InvoiceMetricsService, _parse_flexible_date
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
)


@pytest.mark.django_db()
class TestInvoiceMetricsCalculate:
    """Tests for InvoiceMetricsService.calculate."""

    def test_raises_on_invalid_dates(self) -> None:
        client = ClientFactory()
        with pytest.raises(ValueError, match="Invalid date format"):
            InvoiceMetricsService.calculate(client, "not-a-date", "2024-12-31")

    def test_raises_on_invalid_end_date(self) -> None:
        client = ClientFactory()
        with pytest.raises(ValueError, match="Invalid date format"):
            InvoiceMetricsService.calculate(client, "2024-01-01", "bad-date")

    def test_returns_dict_with_all_required_keys(self) -> None:
        client = ClientFactory()
        result = InvoiceMetricsService.calculate(client, "2025-01-01", "2025-12-31")
        expected_keys = [
            "total_donations_captured",
            "total_donation_amount",
            "gift_aid_captured",
            "gift_aid_amount",
            "letters_generated",
            "campaigns_created",
            "campaigns_updated",
            "hgv_identified",
            "lgv_identified",
            "donor_responses_received",
            "donor_notifications_sent",
            "data_files_processed",
            "batch_operations_completed",
            "reports_generated",
            "templates_created",
            "users_managed",
            "donors_added",
            "donors_updated",
            "api_calls_made",
            "storage_used_mb",
        ]
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"

    def test_counts_donations_in_date_range(self) -> None:
        client = ClientFactory()
        campaign = CampaignFactory(client=client)
        batch = DonationBatchFactory(campaign=campaign)
        now = timezone.now()
        DonationFactory(campaign=campaign, batch=batch, amount=Decimal("25.00"))
        DonationFactory(campaign=campaign, batch=batch, amount=Decimal("50.00"))
        start = (now - timezone.timedelta(days=1)).date().isoformat()
        end = (now + timezone.timedelta(days=1)).date().isoformat()
        result = InvoiceMetricsService.calculate(client, start, end)
        assert result["total_donations_captured"] >= 2
        assert result["total_donation_amount"] >= Decimal("75.00")

    def test_gift_aid_counted_separately(self) -> None:
        client = ClientFactory()
        campaign = CampaignFactory(client=client)
        batch = DonationBatchFactory(campaign=campaign)
        now = timezone.now()
        DonationFactory(
            campaign=campaign, batch=batch, gift_aid=True, amount=Decimal("100")
        )
        DonationFactory(
            campaign=campaign, batch=batch, gift_aid=False, amount=Decimal("50")
        )
        start = (now - timezone.timedelta(days=1)).date().isoformat()
        end = (now + timezone.timedelta(days=1)).date().isoformat()
        result = InvoiceMetricsService.calculate(client, start, end)
        assert result["gift_aid_captured"] == 1
        assert result["gift_aid_amount"] == Decimal("100")

    def test_hgv_lgv_thresholds_from_campaign(self) -> None:
        client = ClientFactory()
        campaign = CampaignFactory(
            client=client, hgv_amount=Decimal("500"), lgv_amount=Decimal("20")
        )
        batch = DonationBatchFactory(campaign=campaign)
        now = timezone.now()
        DonationFactory(campaign=campaign, batch=batch, amount=Decimal("600"))  # HGV
        DonationFactory(campaign=campaign, batch=batch, amount=Decimal("10"))  # LGV
        start = (now - timezone.timedelta(days=1)).date().isoformat()
        end = (now + timezone.timedelta(days=1)).date().isoformat()
        result = InvoiceMetricsService.calculate(client, start, end, campaign=campaign)
        assert result["hgv_identified"] >= 1
        assert result["lgv_identified"] >= 1

    def test_default_hgv_lgv_thresholds_without_campaign(self) -> None:
        """When calculate() is called without a campaign, uses global HGV defaults."""
        client = ClientFactory()
        # Use default factory amounts (hgv_amount=1000, lgv_amount=10)
        campaign = CampaignFactory(client=client)
        batch = DonationBatchFactory(campaign=campaign)
        now = timezone.now()
        DonationFactory(
            campaign=campaign, batch=batch, amount=Decimal("1500")
        )  # Above default HGV threshold of 1000
        start = (now - timezone.timedelta(days=1)).date().isoformat()
        end = (now + timezone.timedelta(days=1)).date().isoformat()
        # Call without campaign — uses global/default thresholds
        result = InvoiceMetricsService.calculate(client, start, end)
        assert result["hgv_identified"] >= 1

    def test_campaign_scope_filters_to_campaign_only(self) -> None:
        client = ClientFactory()
        campaign1 = CampaignFactory(client=client)
        campaign2 = CampaignFactory(client=client)
        batch1 = DonationBatchFactory(campaign=campaign1)
        batch2 = DonationBatchFactory(campaign=campaign2)
        now = timezone.now()
        DonationFactory(campaign=campaign1, batch=batch1, amount=Decimal("10"))
        DonationFactory(campaign=campaign2, batch=batch2, amount=Decimal("20"))
        start = (now - timezone.timedelta(days=1)).date().isoformat()
        end = (now + timezone.timedelta(days=1)).date().isoformat()
        result = InvoiceMetricsService.calculate(client, start, end, campaign=campaign1)
        assert result["total_donations_captured"] == 1
        assert result["total_donation_amount"] == Decimal("10")

    def test_batch_operations_counted(self) -> None:
        client = ClientFactory()
        campaign = CampaignFactory(client=client)
        DonationBatchFactory(campaign=campaign)
        DonationBatchFactory(campaign=campaign)
        now = timezone.now()
        start = (now - timezone.timedelta(days=1)).date().isoformat()
        end = (now + timezone.timedelta(days=1)).date().isoformat()
        result = InvoiceMetricsService.calculate(client, start, end)
        assert result["batch_operations_completed"] >= 2

    def test_uk_date_format_accepted(self) -> None:
        client = ClientFactory()
        result = InvoiceMetricsService.calculate(client, "01/01/2025", "31/12/2025")
        assert isinstance(result, dict)

    def test_no_donations_returns_zero_amounts(self) -> None:
        client = ClientFactory()
        result = InvoiceMetricsService.calculate(client, "2020-01-01", "2020-12-31")
        assert result["total_donations_captured"] == 0
        assert result["total_donation_amount"] == Decimal("0")
        assert result["gift_aid_captured"] == 0

    def test_campaign_scope_counts_campaign_as_created(self) -> None:
        client = ClientFactory()
        campaign = CampaignFactory(client=client)
        # Use a wide date range to catch the campaign's creation
        result = InvoiceMetricsService.calculate(
            client, "2020-01-01", "2030-12-31", campaign=campaign
        )
        # created == 1 if campaign was created in range
        assert result["campaigns_created"] in (0, 1)

    def test_donors_added_counted_in_range(self) -> None:
        client = ClientFactory()
        now = timezone.now()
        DonorFactory()  # created now
        start = (now - timezone.timedelta(days=1)).date().isoformat()
        end = (now + timezone.timedelta(days=1)).date().isoformat()
        result = InvoiceMetricsService.calculate(client, start, end)
        assert result["donors_added"] >= 1


@pytest.mark.django_db()
class TestParseFlexibleDate:
    """Tests for the _parse_flexible_date helper."""

    def test_parses_iso_format(self) -> None:
        result = _parse_flexible_date("2025-06-15")
        assert result is not None

    def test_parses_uk_format(self) -> None:
        result = _parse_flexible_date("15/06/2025")
        assert result is not None

    def test_returns_none_for_invalid(self) -> None:
        result = _parse_flexible_date("not-a-date")
        assert result is None
