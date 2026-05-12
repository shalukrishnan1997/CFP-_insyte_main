"""Additional tests for letters.tasks covering filters, progress, and task entry points."""

import uuid
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from letters.models import LetterBatch, LetterTemplate
from letters.tasks import (
    _complete_batch,
    _filter_currency,
    _filter_date_format,
    _filter_to_float,
    _filter_to_int,
    _run_letter_generation,
    _update_batch_progress,
    cancel_letter_batch,
    generate_letter_batch_task,
    get_donor_context,
    reset_failed_donations,
)
from tests.factories import CampaignFactory, DonationFactory, UserFactory


def _make_letter_template(campaign: Any, user: Any) -> LetterTemplate:
    """Create a minimal LetterTemplate for testing."""
    return LetterTemplate.objects.create(
        campaign=campaign,
        file=SimpleUploadedFile("template.docx", b"fake content"),
        created_by=user,
    )


def _make_letter_batch(
    campaign: Any,
    template: LetterTemplate,
    status: str = "pending",
    batch_number: int = 1,
) -> LetterBatch:
    """Create a minimal LetterBatch for testing."""
    return LetterBatch.objects.create(
        campaign=campaign,
        template=template,
        batch_number=batch_number,
        status=status,
    )


@pytest.fixture
def user(db: Any) -> Any:
    return UserFactory()


@pytest.fixture
def campaign(db: Any) -> Any:
    return CampaignFactory()


@pytest.fixture
def letter_template(db: Any, campaign: Any, user: Any) -> LetterTemplate:
    return _make_letter_template(campaign, user)


class TestFilterDateFormat:
    """Tests for _filter_date_format."""

    def test_none_returns_empty_string(self):
        assert _filter_date_format(None) == ""

    def test_date_object_default_format(self):
        d = date(2024, 1, 15)
        assert _filter_date_format(d) == "15 January 2024"

    def test_date_object_custom_format(self):
        d = date(2024, 3, 5)
        assert _filter_date_format(d, "%d/%m/%Y") == "05/03/2024"

    def test_non_date_value_returns_str(self):
        assert _filter_date_format("not-a-date") == "not-a-date"

    def test_integer_value_returns_str(self):
        assert _filter_date_format(42) == "42"


class TestFilterCurrency:
    """Tests for _filter_currency."""

    def test_none_returns_zero_formatted(self):
        assert _filter_currency(None) == "£0.00"

    def test_float_value_formatted_correctly(self):
        assert _filter_currency(25.5) == "£25.50"

    def test_large_integer_with_comma(self):
        assert _filter_currency(1000) == "£1,000.00"

    def test_invalid_string_returns_original(self):
        assert _filter_currency("bad") == "bad"

    def test_custom_currency_symbol(self):
        assert _filter_currency(10.0, "$") == "$10.00"


class TestFilterToInt:
    """Tests for _filter_to_int."""

    def test_float_truncates_to_int(self):
        assert _filter_to_int(3.7) == 3

    def test_invalid_string_returns_zero(self):
        assert _filter_to_int("bad") == 0

    def test_none_returns_zero(self):
        assert _filter_to_int(None) == 0

    def test_numeric_string_converts(self):
        assert _filter_to_int("5") == 5


class TestFilterToFloat:
    """Tests for _filter_to_float."""

    def test_integer_returns_two_decimal_places(self):
        assert _filter_to_float(3) == "3.00"

    def test_invalid_string_returns_zero(self):
        assert _filter_to_float("bad") == "0.00"

    def test_none_returns_zero(self):
        assert _filter_to_float(None) == "0.00"

    def test_custom_decimal_places(self):
        assert _filter_to_float(3.14159, 4) == "3.1416"


@pytest.mark.django_db
class TestGetDonorContextDonationDateNone:
    """Cover the else-'' branch when donation_date is None."""

    def test_donation_date_none_produces_empty_string(self):
        donation = DonationFactory(donation_date=None)
        ctx = get_donor_context(donation)
        assert ctx["donation_date"] == ""
        assert ctx["donation_date_obj"] is None


class TestCompleteBatch:
    """Direct unit tests for _complete_batch helper."""

    def test_sets_all_batch_fields(self):
        batch = MagicMock()
        _complete_batch(batch, 5, 2, ["file1.docx", "file2.docx"], ["err1"])
        assert batch.status == "completed"
        assert batch.progress_percent == 100
        assert batch.generated_count == 5
        assert batch.failed_count == 2
        assert batch.file_count == 2
        assert batch.output_files == ["file1.docx", "file2.docx"]
        assert batch.error_log == ["err1"]
        batch.save.assert_called_once()

    def test_empty_outputs_allowed(self):
        batch = MagicMock()
        _complete_batch(batch, 0, 0, [], [])
        assert batch.file_count == 0
        assert batch.error_log == []


class TestUpdateBatchProgress:
    """Direct unit tests for _update_batch_progress helper."""

    def test_calculates_progress_percentage(self):
        batch = MagicMock()
        task = MagicMock()
        _update_batch_progress(
            batch,
            task,
            "batch-xyz",
            generated_total=10,
            failed_total=2,
            output_files=["a.docx", "b.docx"],
            processed_so_far=50,
            total_to_process=100,
            group_label="THANKS",
        )
        assert batch.generated_count == 10
        assert batch.failed_count == 2
        assert batch.progress_percent == 50
        assert batch.file_count == 2
        batch.save.assert_called_once()
        task.update_state.assert_called_once()

    def test_zero_total_gives_100_percent(self):
        batch = MagicMock()
        task = MagicMock()
        _update_batch_progress(
            batch,
            task,
            "batch-xyz",
            generated_total=0,
            failed_total=0,
            output_files=[],
            processed_so_far=0,
            total_to_process=0,
            group_label="THANKS",
        )
        assert batch.progress_percent == 100


class TestRunLetterGenerationEmpty:
    """Cover the early-return when donation count is zero."""

    def test_no_donations_returns_success_with_zero_total(self):
        batch = MagicMock()
        task = MagicMock()
        mock_qs = MagicMock()
        mock_qs.count.return_value = 0

        result = _run_letter_generation(batch, task, str(uuid.uuid4()), mock_qs)

        assert result["success"] is True
        assert result["total"] == 0
        assert "No donations" in result["message"]
        batch.save.assert_called()


@pytest.mark.django_db
class TestGenerateLetterBatchTaskPaths:
    """Cover the not-found and already-processing short-circuit paths."""

    def test_unknown_batch_id_returns_not_found(self):
        result = generate_letter_batch_task(str(uuid.uuid4()))
        assert result == {"success": False, "error": "Batch not found"}

    def test_already_processing_returns_error(
        self, campaign: Any, letter_template: LetterTemplate
    ) -> None:
        batch = _make_letter_batch(campaign, letter_template, status="processing")
        result = generate_letter_batch_task(str(batch.id))
        assert result == {"success": False, "error": "Batch is already processing"}


@pytest.mark.django_db
class TestCancelLetterBatch:
    """Cover all branches of cancel_letter_batch."""

    def test_not_found_returns_error(self):
        result = cancel_letter_batch(str(uuid.uuid4()))
        assert result == {"success": False, "error": "Batch not found"}

    def test_already_completed_returns_error(
        self, campaign: Any, letter_template: LetterTemplate
    ) -> None:
        batch = _make_letter_batch(campaign, letter_template, status="completed")
        result = cancel_letter_batch(str(batch.id))
        assert result["success"] is False
        assert "already completed" in result["error"]

    def test_already_cancelled_returns_error(
        self, campaign: Any, letter_template: LetterTemplate
    ) -> None:
        batch = _make_letter_batch(campaign, letter_template, status="cancelled")
        result = cancel_letter_batch(str(batch.id))
        assert result["success"] is False
        assert "already cancelled" in result["error"]

    def test_pending_batch_gets_cancelled(
        self, campaign: Any, letter_template: LetterTemplate
    ) -> None:
        batch = _make_letter_batch(campaign, letter_template, status="pending")
        result = cancel_letter_batch(str(batch.id))
        assert result == {"success": True, "message": "Batch cancelled"}
        batch.refresh_from_db()
        assert batch.status == "cancelled"
        assert batch.completed_at is not None


@pytest.mark.django_db
class TestResetFailedDonations:
    """Tests for reset_failed_donations task."""

    def test_no_failed_donations_returns_zero(self):
        campaign = CampaignFactory()
        result = reset_failed_donations(str(campaign.id))
        assert result == {"success": True, "reset_count": 0}

    def test_resets_only_failed_donations(self):
        campaign = CampaignFactory()
        DonationFactory(campaign=campaign, letter_status="failed")
        DonationFactory(campaign=campaign, letter_status="failed")
        DonationFactory(campaign=campaign, letter_status="pending")

        result = reset_failed_donations(str(campaign.id))
        assert result["success"] is True
        assert result["reset_count"] == 2
