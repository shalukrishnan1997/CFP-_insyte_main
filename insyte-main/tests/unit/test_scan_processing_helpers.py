"""Tests for scan processing helper functions (donations + OCR modules)."""

from decimal import Decimal
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from campaigns.models import CampaignDataFile
from donors.models import DataFileDonor
from tests.factories import (
    DonationBatchFactory,
    DonorFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    SystemDonorFactory,
)


class TestParseExtractedAmount:
    """Tests for scan_processing_donations.parse_extracted_amount."""

    def test_returns_zero_for_empty_amount(self) -> None:
        from scans.scan_processing_donations import parse_extracted_amount

        result = parse_extracted_amount({})
        assert result.value == Decimal("0.00")
        assert result.parse_failed is False
        assert result.raw == ""

    def test_parses_valid_amount(self) -> None:
        from scans.scan_processing_donations import parse_extracted_amount

        result = parse_extracted_amount({"amount": "25.50"})
        assert result.value == Decimal("25.50")
        assert result.parse_failed is False
        assert result.raw == "25.50"

    def test_flags_parse_failure_for_invalid_amount(self) -> None:
        from scans.scan_processing_donations import parse_extracted_amount

        result = parse_extracted_amount({"amount": "not_a_number"})
        assert result.value == Decimal("0.00")
        assert result.parse_failed is True
        assert result.raw == "not_a_number"

    def test_returns_zero_without_failure_for_empty_string(self) -> None:
        from scans.scan_processing_donations import parse_extracted_amount

        result = parse_extracted_amount({"amount": ""})
        assert result.value == Decimal("0.00")
        assert result.parse_failed is False

    def test_parses_explicit_zero_without_failure(self) -> None:
        from scans.scan_processing_donations import parse_extracted_amount

        result = parse_extracted_amount({"amount": "0"})
        assert result.value == Decimal("0")
        assert result.parse_failed is False
        assert result.raw == "0"


class TestParseExtractedDate:
    """Tests for scan_processing_donations.parse_extracted_date."""

    def test_returns_none_for_empty_date(self) -> None:
        from scans.scan_processing_donations import parse_extracted_date

        result = parse_extracted_date({})
        assert result is None

    def test_returns_none_for_empty_string_date(self) -> None:
        from scans.scan_processing_donations import parse_extracted_date

        result = parse_extracted_date({"donation_date": ""})
        assert result is None

    def test_parses_valid_date(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import parse_extracted_date

        result = parse_extracted_date({"donation_date": "01/06/2024"})
        assert result == date(2024, 6, 1)

    def test_returns_none_for_invalid_date(self) -> None:
        from scans.scan_processing_donations import parse_extracted_date

        result = parse_extracted_date({"donation_date": "not_a_date"})
        assert result is None


class TestResolveDonorSource:
    """Tests for scan_processing_donations.resolve_donor_source."""

    def test_returns_data_file_when_campaign_is_data_file_and_matched(self) -> None:
        from scans.scan_processing_donations import resolve_donor_source

        campaign = MagicMock()
        campaign.donor_source = "data_file"
        placeholder = MagicMock()
        placeholder.matched_data_file_donor = MagicMock()

        result = resolve_donor_source(placeholder, campaign)
        assert result == "data_file"

    def test_returns_house_file_when_donor_matched(self) -> None:
        from scans.scan_processing_donations import resolve_donor_source

        campaign = MagicMock()
        campaign.donor_source = "data_file"
        placeholder = MagicMock()
        placeholder.matched_data_file_donor = None
        placeholder.matched_donor = MagicMock()

        result = resolve_donor_source(placeholder, campaign)
        assert result == "house_file"

    def test_prefers_data_file_when_both_matches_exist(self) -> None:
        from scans.scan_processing_donations import resolve_donor_source

        campaign = MagicMock()
        campaign.donor_source = "data_file"
        placeholder = MagicMock()
        placeholder.matched_data_file_donor = MagicMock()
        placeholder.matched_donor = MagicMock()

        result = resolve_donor_source(placeholder, campaign)
        assert result == "data_file"

    def test_returns_campaign_donor_source_as_fallback(self) -> None:
        from scans.scan_processing_donations import resolve_donor_source

        campaign = MagicMock()
        campaign.donor_source = "cold"
        placeholder = MagicMock()
        placeholder.matched_data_file_donor = None
        placeholder.matched_donor = None
        placeholder.matched_system_donor = None

        result = resolve_donor_source(placeholder, campaign)
        assert result == "cold"


class TestBuildConfidenceDict:
    """Tests for scan_processing_donations.build_confidence_dict."""

    def test_returns_empty_for_no_confidence_fields(self) -> None:
        from scans.scan_processing_donations import build_confidence_dict

        result = build_confidence_dict({"amount": "10.00", "donor_name": "John"})
        assert result == {}

    def test_extracts_confidence_values(self) -> None:
        from scans.scan_processing_donations import build_confidence_dict

        result = build_confidence_dict(
            {
                "amount": "10.00",
                "amount_confidence": 0.95,
                "donor_name": "John",
                "donor_name_confidence": 0.80,
            }
        )
        assert result["amount"] == 0.95
        assert result["donor_name"] == 0.80

    def test_skips_zero_confidence_values(self) -> None:
        from scans.scan_processing_donations import build_confidence_dict

        result = build_confidence_dict({"amount_confidence": 0.0})
        assert "amount" not in result


class TestParseDateField:
    """Tests for scan_processing_donations.parse_date_field."""

    def test_returns_none_for_empty(self) -> None:
        from scans.scan_processing_donations import parse_date_field

        assert parse_date_field("") is None
        assert parse_date_field(None) is None  # type: ignore[arg-type]

    def test_parses_valid_date(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import parse_date_field

        result = parse_date_field("15/03/2024")
        assert result == date(2024, 3, 15)

    def test_parses_two_digit_year_date(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import parse_date_field

        result = parse_date_field("3-3-26")
        assert result == date(2026, 3, 3)

    def test_returns_none_for_invalid_string(self) -> None:
        from scans.scan_processing_donations import parse_date_field

        result = parse_date_field("garbage_text")
        assert result is None


class TestParseDecimalField:
    """Tests for scan_processing_donations.parse_decimal_field."""

    def test_returns_zero_for_empty(self) -> None:
        from scans.scan_processing_donations import parse_decimal_field

        assert parse_decimal_field("") == Decimal("0.00")

    def test_parses_valid_decimal(self) -> None:
        from scans.scan_processing_donations import parse_decimal_field

        assert parse_decimal_field("123.45") == Decimal("123.45")

    def test_returns_zero_for_invalid(self) -> None:
        from scans.scan_processing_donations import parse_decimal_field

        assert parse_decimal_field("not_a_number") == Decimal("0.00")


@pytest.mark.django_db()
class TestCreateDonationFromPlaceholder:
    """Regression coverage for payment-field normalization during donation creation."""

    def test_preserves_matched_data_file_donor_link(self) -> None:
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        scan_batch = ScanBatchFactory(
            payment_method="cheque", campaign__donor_source="data_file"
        )
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        data_file = CampaignDataFile.objects.create(
            campaign=scan_batch.campaign,
            created_by=scan_batch.created_by,
        )
        data_file_donor = DataFileDonor.objects.create(
            data_file=data_file,
            client=scan_batch.campaign.client,
            urn="URN-DATA-001",
            first_name="Dora",
            last_name="Data",
        )
        system_donor = SystemDonorFactory(
            client=scan_batch.campaign.client,
            external_urn="",
        )
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=None,
            matched_data_file_donor=data_file_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            qr_decoded=True,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={"amount": "15.00"},
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.donor_source == "data_file"
        assert donation.data_file_donor == data_file_donor
        assert donation.donor is None
        assert donation.system_donor == system_donor

    def test_cheque_batch_ignores_card_fields_in_extracted_data(self) -> None:
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            qr_decoded=True,
            ocr_data={
                "campaign_temperature": "warm",
                "identifier_source": "qr",
                "donor_match_status": "matched",
                "exception_reason": "",
            },
            extracted_data={
                "amount": "15.00",
                "gift_aid": True,
                "cheque_number": "CHQ12345",
                "cheque_date": "07/06/2024",
                "card_holder_name": "Template Label",
                "card_last_four": "4242",
                "card_expiry_date": "00/00",
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.payment_method == "cheque"
        assert donation.system_donor == system_donor
        assert donation.cheque_number == "CHQ12345"
        assert str(donation.cheque_date) == "2024-06-07"
        assert donation.card_holder_name == ""
        assert donation.card_last_four == ""
        assert donation.card_expiry_date == ""

    def test_postal_order_batch_reuses_generic_cheque_number_field(self) -> None:
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="postal_order")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "12.50",
                "cheque_number": "PO-7788",
                "card_expiry_date": "00/00",
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.payment_method == "postal_order"
        assert donation.postal_order_number == "PO-7788"
        assert donation.cheque_number == ""
        assert donation.card_expiry_date == ""

    def test_direct_debit_persists_encrypted_bank_fields(self) -> None:
        """Sort code + account number persist encrypted; raw JSON gets redacted."""
        from django.db import connection

        from donations.models import Donation
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(
            payment_method="direct_debit", scan_form_type="simplex"
        )
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "10.00",
                "payment_method": "direct_debit",
                "sort_code": "12-34-56",
                "account_number": "1234 5678",
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.payment_method == "direct_debit"
        assert donation.sort_code == "123456"
        assert donation.account_number == "12345678"

        # Verify the column stores ciphertext, not the plaintext digits.
        # SQLite stores UUIDField as a 32-char hex string (no dashes), so
        # query by the hex form rather than the str(uuid) representation.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sort_code, account_number FROM donations_donation WHERE id = %s",
                [donation.id.hex],
            )
            row = cursor.fetchone()
        assert row is not None
        raw_sort_code, raw_account_number = row
        assert raw_sort_code != "123456"
        assert raw_account_number != "12345678"
        assert raw_sort_code  # not empty
        assert raw_account_number

        # ORM read decrypts back to plaintext.
        refreshed = Donation.objects.get(pk=donation.id)
        assert refreshed.sort_code == "123456"
        assert refreshed.account_number == "12345678"

        # Plaintext bank PII removed from extracted_data after persistence.
        placeholder.refresh_from_db()
        assert placeholder.extracted_data["sort_code"] == "[REDACTED]"
        assert placeholder.extracted_data["account_number"] == "[REDACTED]"

    def test_direct_debit_truncates_to_uk_bank_field_widths(self) -> None:
        """Sort code is capped at 6 digits and account number at 8 digits.

        UK sort codes are exactly 6 digits and UK domestic account numbers are
        exactly 8 digits. ``_digits_only`` truncates at those limits so a
        malformed OCR read (e.g. an extra digit picked up from a neighbouring
        field) cannot silently persist a value that looks valid.
        """
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(
            payment_method="direct_debit", scan_form_type="simplex"
        )
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "10.00",
                "payment_method": "direct_debit",
                # 8 digits in the OCR — must truncate to the leading 6.
                "sort_code": "12345678",
                # 12 digits in the OCR — must truncate to the leading 8.
                "account_number": "123456789012",
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.sort_code == "123456"
        assert len(donation.sort_code or "") == 6
        assert donation.account_number == "12345678"
        assert len(donation.account_number or "") == 8

    def test_non_direct_debit_does_not_set_bank_fields(self) -> None:
        """Cheque batches should not surface OCR bank digits in the donation."""
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "20.00",
                "sort_code": "11-22-33",
                "account_number": "99887766",
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.payment_method == "cheque"
        # Empty values round-trip as either "" or None depending on the
        # encrypted-field implementation; both indicate "no value stored".
        assert not donation.sort_code
        assert not donation.account_number
        # Non-direct-debit placeholders keep the raw extracted_data untouched.
        placeholder.refresh_from_db()
        assert placeholder.extracted_data["sort_code"] == "11-22-33"
        assert placeholder.extracted_data["account_number"] == "99887766"

    def test_cheque_batch_uses_generic_donation_date_as_cheque_date(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "15.00",
                "donation_date": "3-3-26",
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.payment_method == "cheque"
        assert donation.donation_date == date(2026, 3, 3)
        assert donation.cheque_date == date(2026, 3, 3)

    def test_low_per_field_amount_confidence_flags_donation(self) -> None:
        from donations.models import Donation
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "15.00",
                "amount_confidence": 0.10,
                "urn_confidence": 0.95,
                "payment_method_confidence": 0.95,
                "donation_date_confidence": 0.95,
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert "Low OCR confidence on: amount" in donation.qa_notes

    def test_high_per_field_confidence_does_not_flag_donation(self) -> None:
        from donations.models import Donation
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "15.00",
                "cheque_date": "01/05/2026",
                "amount_confidence": 0.95,
                "urn_confidence": 0.95,
                "payment_method_confidence": 0.95,
                "donation_date_confidence": 0.95,
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.qa_notes == ""

    def test_multiple_low_confidence_fields_listed_in_notes(self) -> None:
        from donations.models import Donation
        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "15.00",
                "amount_confidence": 0.10,
                "urn_confidence": 0.20,
                "payment_method_confidence": 0.95,
                "donation_date_confidence": 0.95,
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert "amount" in donation.qa_notes
        assert "donor_urn" in donation.qa_notes


class TestLowConfidenceFields:
    """Tests for scan_processing_donations._low_confidence_fields."""

    def test_returns_empty_for_no_extracted_data(self) -> None:
        from scans.scan_processing_donations import (
            CRITICAL_DONATION_FIELDS,
            _low_confidence_fields,
        )

        placeholder = MagicMock()
        placeholder.extracted_data = None

        assert _low_confidence_fields(placeholder, CRITICAL_DONATION_FIELDS, 0.30) == []

    def test_skips_missing_confidence_keys(self) -> None:
        from scans.scan_processing_donations import (
            CRITICAL_DONATION_FIELDS,
            _low_confidence_fields,
        )

        placeholder = MagicMock()
        placeholder.extracted_data = {"amount": "10.00"}

        assert _low_confidence_fields(placeholder, CRITICAL_DONATION_FIELDS, 0.30) == []

    def test_skips_zero_confidence_no_signal(self) -> None:
        from scans.scan_processing_donations import (
            CRITICAL_DONATION_FIELDS,
            _low_confidence_fields,
        )

        placeholder = MagicMock()
        placeholder.extracted_data = {"amount_confidence": 0.0}

        assert _low_confidence_fields(placeholder, CRITICAL_DONATION_FIELDS, 0.30) == []

    def test_returns_field_names_below_threshold(self) -> None:
        from scans.scan_processing_donations import (
            CRITICAL_DONATION_FIELDS,
            _low_confidence_fields,
        )

        placeholder = MagicMock()
        placeholder.extracted_data = {
            "amount_confidence": 0.10,
            "urn_confidence": 0.95,
            "payment_method_confidence": 0.20,
            "donation_date_confidence": 0.95,
        }

        result = _low_confidence_fields(placeholder, CRITICAL_DONATION_FIELDS, 0.30)
        assert result == ["amount", "payment_method"]


class TestResolveDonationDate:
    """Tests for scan_processing_donations._resolve_donation_date."""

    def test_uses_generic_donation_date_when_present(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import _resolve_donation_date

        result, used_fallback = _resolve_donation_date(
            {"donation_date": "15/03/2024"}, "card"
        )
        assert result == date(2024, 3, 15)
        assert used_fallback is False

    def test_prefers_cheque_date_for_cheque_payments(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import _resolve_donation_date

        result, used_fallback = _resolve_donation_date(
            {"donation_date": "15/03/2024", "cheque_date": "07/06/2024"},
            "cheque",
        )
        assert result == date(2024, 6, 7)
        assert used_fallback is False

    def test_falls_back_to_generic_when_cheque_date_missing(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import _resolve_donation_date

        result, used_fallback = _resolve_donation_date(
            {"donation_date": "15/03/2024"}, "cheque"
        )
        assert result == date(2024, 3, 15)
        assert used_fallback is False

    def test_prefers_postal_order_date_for_postal_orders(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import _resolve_donation_date

        result, used_fallback = _resolve_donation_date(
            {
                "donation_date": "15/03/2024",
                "cheque_date": "07/06/2024",
                "postal_order_date": "20/12/2024",
            },
            "postal_order",
        )
        assert result == date(2024, 12, 20)
        assert used_fallback is False

    def test_postal_order_falls_back_to_cheque_date(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import _resolve_donation_date

        result, used_fallback = _resolve_donation_date(
            {"donation_date": "15/03/2024", "cheque_date": "07/06/2024"},
            "postal_order",
        )
        assert result == date(2024, 6, 7)
        assert used_fallback is False

    def test_uses_today_when_no_dates_present(self) -> None:
        from django.utils import timezone

        from scans.scan_processing_donations import _resolve_donation_date

        result, used_fallback = _resolve_donation_date({}, "card")
        assert result == timezone.now().date()
        assert used_fallback is True

    def test_uses_today_when_dates_unparseable(self) -> None:
        from django.utils import timezone

        from scans.scan_processing_donations import _resolve_donation_date

        result, used_fallback = _resolve_donation_date(
            {"donation_date": "not_a_date", "cheque_date": "garbage"},
            "cheque",
        )
        assert result == timezone.now().date()
        assert used_fallback is True


@pytest.mark.django_db()
class TestDonationDatePersistence:
    """End-to-end coverage for OCR donation date wiring on Donation creation."""

    def test_card_donation_persists_extracted_donation_date(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="card", scan_form_type="simplex")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "20.00",
                "donation_date": "12/05/2024",
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.donation_date == date(2024, 5, 12)
        assert donation.qa_status == donation.QA_STATUS_PENDING
        assert "Donation date defaulted" not in donation.qa_notes

    def test_postal_order_uses_postal_order_date_over_generic(self) -> None:
        from datetime import date

        from scans.scan_processing_donations import (
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="postal_order")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "12.50",
                "donation_date": "15/03/2024",
                "postal_order_date": "20/12/2024",
                "cheque_number": "PO-7788",
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.donation_date == date(2024, 12, 20)
        assert donation.postal_order_date == date(2024, 12, 20)

    def test_missing_extracted_date_flags_donation_and_appends_note(self) -> None:
        from django.utils import timezone

        from scans.scan_processing_donations import (
            DATE_FALLBACK_NOTE,
            create_donation_from_placeholder,
        )

        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="card", scan_form_type="simplex")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={"amount": "20.00"},
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.donation_date == timezone.now().date()
        assert donation.qa_status == donation.QA_STATUS_FLAGGED
        assert DATE_FALLBACK_NOTE in donation.qa_notes


class TestMergeExtractedMultiPage:
    """Tests for scan_processing_ocr.merge_extracted_multi_page."""

    def test_returns_empty_for_no_pages(self) -> None:
        from scans.scan_processing_ocr import merge_extracted_multi_page

        result = merge_extracted_multi_page([])
        assert result == {}

    def test_returns_single_page_unchanged(self) -> None:
        from scans.scan_processing_ocr import merge_extracted_multi_page

        pages = [{"amount": "10.00", "donor_name": "John"}]
        result = merge_extracted_multi_page(pages)
        assert result == {"amount": "10.00", "donor_name": "John"}

    def test_merges_two_pages_preferring_non_empty(self) -> None:
        from scans.scan_processing_ocr import merge_extracted_multi_page

        pages = [
            {"amount": "10.00", "donor_name": ""},
            {"amount": "", "donor_name": "John Smith"},
        ]
        result = merge_extracted_multi_page(pages)
        assert result["amount"] == "10.00"
        assert result["donor_name"] == "John Smith"

    def test_gift_aid_true_propagates(self) -> None:
        from scans.scan_processing_ocr import merge_extracted_multi_page

        pages = [
            {"gift_aid": False},
            {"gift_aid": True},
        ]
        result = merge_extracted_multi_page(pages)
        assert result["gift_aid"] is True

    def test_averages_confidence_across_pages(self) -> None:
        from scans.scan_processing_ocr import merge_extracted_multi_page

        pages = [
            {"amount_confidence": 0.9},
            {"amount_confidence": 0.7},
        ]
        result = merge_extracted_multi_page(pages)
        # The confidence should be the average (0.8)
        assert "amount_confidence" in result
        assert abs(result["amount_confidence"] - 0.8) < 0.01

    def test_prefers_later_page_amount_when_first_page_is_low_value_misread(
        self,
    ) -> None:
        from scans.scan_processing_ocr import merge_extracted_multi_page

        pages = [
            {"amount": "1", "amount_confidence": 0.6},
            {"amount": "12", "amount_confidence": 0.6},
        ]

        result = merge_extracted_multi_page(pages)

        assert result["amount"] == "12"


class TestDecodeQRFromScan:
    """Tests for scan_processing_ocr.decode_qr_from_scan."""

    @patch("scans.qr.QRService.decode")
    def test_returns_none_when_qr_absent(self, mock_decode: MagicMock) -> None:
        from scans.qr import QRResult, QRResultKind
        from scans.scan_processing_ocr import decode_qr_from_scan

        mock_decode.return_value = QRResult(kind=QRResultKind.ABSENT)
        placeholder = MagicMock()
        placeholder.ocr_data = {}
        result = decode_qr_from_scan(placeholder, b"fake_image")

        assert result is None
        assert placeholder.qr_decoded is False
        assert placeholder.ocr_data["qr_kind"] == "absent"

    @patch("scans.qr.QRService.decode")
    def test_returns_none_and_records_error_when_malformed(
        self, mock_decode: MagicMock
    ) -> None:
        from scans.qr import QRResult, QRResultKind
        from scans.scan_processing_ocr import decode_qr_from_scan

        mock_decode.return_value = QRResult(
            kind=QRResultKind.MALFORMED, error="bad payload"
        )
        placeholder = MagicMock()
        placeholder.ocr_data = {}
        result = decode_qr_from_scan(placeholder, b"fake_image")

        assert result is None
        assert placeholder.qr_decoded is False
        assert placeholder.ocr_data["qr_kind"] == "malformed"
        assert placeholder.ocr_data["qr_error"] == "bad payload"

    @patch("scans.qr.QRService.encode_payload", return_value="RAW")
    @patch("scans.qr.QRService.decode")
    def test_returns_qr_data_and_updates_placeholder(
        self, mock_decode: MagicMock, mock_encode: MagicMock
    ) -> None:
        from scans.qr import QRResult, QRResultKind
        from scans.scan_processing_ocr import decode_qr_from_scan

        mock_decode.return_value = QRResult(
            kind=QRResultKind.DECODED,
            appeal_code="APPEAL1",
            package_code="PKG1",
            urn="URN123",
        )

        placeholder = MagicMock()
        placeholder.ocr_data = {}
        result = decode_qr_from_scan(placeholder, b"fake_image")

        assert result == {
            "appeal_code": "APPEAL1",
            "package_code": "PKG1",
            "urn": "URN123",
        }
        assert placeholder.qr_decoded is True
        assert placeholder.qr_raw == "RAW"
        assert placeholder.ocr_data["qr_kind"] == "decoded"


class TestBuildPdfBytesFromR2Keys:
    """Tests for mixed image/PDF donor PDF assembly helpers."""

    def test_build_pdf_bytes_from_image_keys_returns_pdf(self) -> None:
        from PIL import Image
        from pypdf import PdfReader

        from scans.scan_processing_r2 import build_pdf_bytes_from_r2_keys

        png_one = BytesIO()
        Image.new("RGB", (120, 120), color="white").save(png_one, format="PNG")
        png_two = BytesIO()
        Image.new("RGB", (120, 120), color="lightgray").save(png_two, format="PNG")

        with patch(
            "scans.scan_processing_r2.download_r2_bytes",
            side_effect=[png_one.getvalue(), png_two.getvalue()],
        ):
            pdf_bytes = build_pdf_bytes_from_r2_keys(
                [
                    "ScanOutput/demo/split/batch_doc_0001.png",
                    "ScanOutput/demo/split/batch_doc_0002.png",
                ]
            )

        reader = PdfReader(BytesIO(pdf_bytes))
        assert len(reader.pages) == 2


class TestSplitPdfPagesToR2:
    """Tests for image-first PDF split uploads."""

    @override_settings(R2_BUCKET_NAME="test-bucket")
    def test_split_pdf_uploads_png_when_page_contains_scan_image(self) -> None:
        from PIL import Image

        from scans.scan_processing_r2 import _split_pdf_pages_to_r2

        first = Image.new("RGB", (100, 100), color="white")
        second = Image.new("RGB", (100, 100), color="lightgray")
        pdf_buffer = BytesIO()
        first.save(pdf_buffer, format="PDF", save_all=True, append_images=[second])

        mock_r2_client = MagicMock()
        with patch("core.storage_backends.get_r2_client", return_value=mock_r2_client):
            split_keys = _split_pdf_pages_to_r2(
                "ScanOutput/demo/batch.pdf",
                pdf_buffer.getvalue(),
                page_count=2,
            )

        assert split_keys == [
            "ScanOutput/demo/split/batch_doc_0001.png",
            "ScanOutput/demo/split/batch_doc_0002.png",
        ]
        assert mock_r2_client.put_object.call_count == 2
        uploaded_content_types = [
            call.kwargs["ContentType"]
            for call in mock_r2_client.put_object.call_args_list
        ]
        assert uploaded_content_types == ["image/png", "image/png"]

    def test_split_pdf_raises_when_page_has_no_extractable_image(self) -> None:
        from scans.scan_processing_r2 import _upload_split_page_object

        reader = MagicMock()
        mock_r2_client = MagicMock()

        with patch(
            "scans.scan_processing_r2._extract_scanned_page_image",
            return_value=None,
        ):
            try:
                _upload_split_page_object(
                    mock_r2_client,
                    "test-bucket",
                    reader,
                    0,
                    "ScanOutput/demo/split/batch_doc_",
                )
            except RuntimeError as exc:
                assert "could not be extracted as an image" in str(exc)
            else:
                raise AssertionError(
                    "Expected RuntimeError when image extraction fails"
                )

        mock_r2_client.put_object.assert_not_called()


@pytest.mark.django_db()
def test_prepare_scan_batch_allows_ocr_before_redaction(
    redaction_required_all: object,
) -> None:
    """Pending redaction should no longer prevent OCR batch preparation."""
    del redaction_required_all
    from scans.scan_processing import ScanProcessingService

    batch = ScanBatchFactory(status="pending")
    placeholder = ScanPlaceholderFactory(
        batch=batch,
        ocr_status="pending",
        redaction_status="pending",
        image_url="",
        image_path="ScanOutput/demo/ocr_before_redaction.png",
        page_keys=["ScanOutput/demo/ocr_before_redaction.png"],
    )

    prepared_batch, known_urns, placeholder_ids = (
        ScanProcessingService.prepare_scan_batch(str(batch.id))
    )

    batch.refresh_from_db()
    assert prepared_batch.id == batch.id
    assert batch.status == batch.STATUS_PROCESSING
    assert isinstance(known_urns, list)
    assert placeholder_ids == [str(placeholder.id)]


class TestRunQrAndMatchOnly:
    """Tests for the Document-AI-free QR + donor-match path."""

    @patch("scans.scan_processing_ocr.download_r2_bytes", return_value=b"fake_image")
    @patch("scans.qr.QRService.encode_payload", return_value="RAW")
    @patch("scans.qr.QRService.decode")
    def test_sets_urn_from_qr_and_clears_extracted_data(
        self,
        mock_decode: MagicMock,
        _mock_encode: MagicMock,
        _mock_download: MagicMock,
    ) -> None:
        from scans.qr import QRResult, QRResultKind
        from scans.scan_processing_ocr import run_qr_and_match_only

        mock_decode.return_value = QRResult(
            kind=QRResultKind.DECODED,
            appeal_code="APPEAL1",
            package_code="PKG1",
            urn="URN-QR-123",
        )
        placeholder = MagicMock()
        placeholder.image_path = "ScanOutput/demo/card_form_0001.png"
        placeholder.urn = "filename-urn"
        placeholder.ocr_data = {}

        result = run_qr_and_match_only(placeholder, campaign=MagicMock(), known_urns=[])

        assert result == {}
        assert placeholder.extracted_data == {}
        assert placeholder.urn == "URN-QR-123"
        assert placeholder.qr_decoded is True
        assert placeholder.ocr_data["document_ai_skipped"] is True
        assert placeholder.ocr_data["qr_kind"] == "decoded"
        assert placeholder.ocr_confidence == 0.0

    @patch("scans.scan_processing_ocr.download_r2_bytes", return_value=b"fake_image")
    @patch("scans.qr.QRService.decode")
    def test_keeps_filename_urn_when_qr_absent(
        self, mock_decode: MagicMock, _mock_download: MagicMock
    ) -> None:
        from scans.qr import QRResult, QRResultKind
        from scans.scan_processing_ocr import run_qr_and_match_only

        mock_decode.return_value = QRResult(kind=QRResultKind.ABSENT)
        placeholder = MagicMock()
        placeholder.image_path = "ScanOutput/demo/card_form_0001.png"
        placeholder.urn = "filename-fallback"
        placeholder.ocr_data = {}

        result = run_qr_and_match_only(placeholder, campaign=MagicMock(), known_urns=[])

        assert result == {}
        assert placeholder.urn == "filename-fallback"
        assert placeholder.qr_decoded is False
        assert placeholder.ocr_data["document_ai_skipped"] is True
        assert placeholder.ocr_data["qr_kind"] == "absent"

    @patch("scans.scan_processing_ocr.download_r2_bytes", return_value=b"fake_image")
    @patch("scans.qr.QRService.decode")
    def test_does_not_call_document_ai(
        self, mock_decode: MagicMock, _mock_download: MagicMock
    ) -> None:
        """PCI-critical: Document AI must never be invoked on the skip path."""
        from scans.qr import QRResult, QRResultKind
        from scans.scan_processing_ocr import run_qr_and_match_only

        mock_decode.return_value = QRResult(
            kind=QRResultKind.DECODED,
            appeal_code="APPEAL1",
            package_code="",
            urn="URN-999",
        )
        placeholder = MagicMock()
        placeholder.image_path = "ScanOutput/demo/card_form_0001.png"
        placeholder.urn = ""
        placeholder.ocr_data = {}

        with patch(
            "scans.document_ai.DocumentAIService.process_image_bytes"
        ) as mock_doc_ai:
            run_qr_and_match_only(placeholder, campaign=MagicMock(), known_urns=None)
            mock_doc_ai.assert_not_called()


class TestProcessSingleScanSkipsDocumentAi:
    """Tests for scan_processing.process_single_scan routing by payment_method."""

    @pytest.mark.django_db()
    @pytest.mark.parametrize(
        "payment_method",
        ["card", "direct_debit", "caf", "postal_order"],
    )
    @patch("scans.scan_processing.apply_donor_match")
    @patch("scans.scan_processing.run_qr_and_match_only", return_value={})
    @patch("scans.scan_processing.run_ocr_and_extract")
    def test_skip_list_payment_methods_bypass_document_ai(
        self,
        mock_run_ocr: MagicMock,
        mock_qr_only: MagicMock,
        mock_apply_match: MagicMock,
        payment_method: str,
    ) -> None:
        from scans.models import ScanPlaceholder
        from scans.scan_processing import ScanProcessingService

        mock_apply_match.return_value = {
            "donor": None,
            "data_file_donor": None,
            "source": "not_found",
            "donor_name": "",
        }
        scan_form_type = (
            "simplex"
            if payment_method in {"card", "direct_debit"}
            else "simplex_with_payment"
        )
        batch = ScanBatchFactory(
            status="pending",
            payment_method=payment_method,
            scan_form_type=scan_form_type,
        )
        placeholder = ScanPlaceholderFactory(
            batch=batch,
            ocr_status="pending",
            image_url="",
            image_path="ScanOutput/demo/skip_doc_ai.png",
            page_keys=["ScanOutput/demo/skip_doc_ai.png"],
        )

        ScanProcessingService.process_single_scan(str(placeholder.id))

        mock_run_ocr.assert_not_called()
        mock_qr_only.assert_called_once()
        placeholder.refresh_from_db()
        assert placeholder.ocr_status == ScanPlaceholder.OCR_STATUS_SKIPPED

    @pytest.mark.django_db()
    @patch("scans.scan_processing.apply_donor_match")
    @patch("scans.scan_processing.run_qr_and_match_only")
    @patch(
        "scans.scan_processing.run_ocr_and_extract", return_value={"amount": "25.00"}
    )
    def test_cheque_batch_still_runs_document_ai(
        self,
        mock_run_ocr: MagicMock,
        mock_qr_only: MagicMock,
        mock_apply_match: MagicMock,
    ) -> None:
        from scans.scan_processing import ScanProcessingService

        mock_apply_match.return_value = {
            "donor": None,
            "data_file_donor": None,
            "source": "not_found",
            "donor_name": "",
        }
        batch = ScanBatchFactory(
            status="pending",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )
        placeholder = ScanPlaceholderFactory(
            batch=batch,
            ocr_status="pending",
            image_url="",
            image_path="ScanOutput/demo/cheque.png",
            page_keys=["ScanOutput/demo/cheque.png"],
        )

        ScanProcessingService.process_single_scan(str(placeholder.id))

        mock_run_ocr.assert_called_once()
        mock_qr_only.assert_not_called()

    @pytest.mark.django_db()
    @patch("scans.scan_processing.apply_donor_match")
    @patch("scans.scan_processing.run_qr_and_match_only", return_value={})
    def test_skip_path_preserves_matched_status_when_donor_found(
        self,
        _mock_qr_only: MagicMock,
        mock_apply_match: MagicMock,
    ) -> None:
        from scans.models import ScanPlaceholder
        from scans.scan_processing import ScanProcessingService

        def _set_matched_status(placeholder: Any, _campaign: Any) -> dict[str, Any]:
            placeholder.ocr_status = ScanPlaceholder.OCR_STATUS_MATCHED
            return {
                "donor": None,
                "data_file_donor": None,
                "source": "data_file",
                "donor_name": "Alice",
            }

        mock_apply_match.side_effect = _set_matched_status
        batch = ScanBatchFactory(
            status="pending", payment_method="card", scan_form_type="simplex"
        )
        placeholder = ScanPlaceholderFactory(
            batch=batch,
            ocr_status="pending",
            image_url="",
            image_path="ScanOutput/demo/card_matched.png",
            page_keys=["ScanOutput/demo/card_matched.png"],
        )

        ScanProcessingService.process_single_scan(str(placeholder.id))

        placeholder.refresh_from_db()
        assert placeholder.ocr_status == ScanPlaceholder.OCR_STATUS_MATCHED


@pytest.mark.django_db()
def test_create_donation_from_skipped_placeholder_has_blank_fields() -> None:
    """Blank Donation is created for a SKIPPED placeholder with empty extracted data."""
    from scans.models import ScanPlaceholder
    from scans.scan_processing_donations import create_donation_batch

    batch = ScanBatchFactory(
        status="pending",
        payment_method="card",
        scan_form_type="simplex",
    )
    ScanPlaceholderFactory(
        batch=batch,
        ocr_status=ScanPlaceholder.OCR_STATUS_SKIPPED,
        image_url="",
        image_path="ScanOutput/demo/card_blank.png",
        page_keys=["ScanOutput/demo/card_blank.png"],
        extracted_data={},
    )

    donation_batch = create_donation_batch(batch)

    assert donation_batch is not None
    donations = list(donation_batch.donations.all())
    assert len(donations) == 1
    donation = donations[0]
    assert donation.amount == Decimal("0.00")
    assert donation.card_holder_name == ""
    assert donation.card_last_four == ""
    assert not donation.card_expiry_date
