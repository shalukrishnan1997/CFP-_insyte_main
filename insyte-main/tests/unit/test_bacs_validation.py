"""Unit tests for ``donations.bacs_validation``."""

from donations.bacs_validation import (
    REASON_INVALID_FORMAT,
    REASON_PASSED_FORMAT_ONLY,
    normalize_account_number,
    normalize_sort_code,
    validate_bacs,
)


class TestNormalizeSortCode:
    def test_strips_dashes(self) -> None:
        assert normalize_sort_code("60-83-71") == "608371"

    def test_strips_spaces(self) -> None:
        assert normalize_sort_code("60 83 71") == "608371"

    def test_already_canonical(self) -> None:
        assert normalize_sort_code("608371") == "608371"

    def test_empty_or_none(self) -> None:
        assert normalize_sort_code("") == ""
        assert normalize_sort_code(None) == ""


class TestNormalizeAccountNumber:
    def test_pads_short_numbers_to_eight(self) -> None:
        assert normalize_account_number("123456") == "00123456"
        assert normalize_account_number("1234567") == "01234567"

    def test_eight_digit_unchanged(self) -> None:
        assert normalize_account_number("12345678") == "12345678"

    def test_strips_separators(self) -> None:
        assert normalize_account_number("12 34-56-78") == "12345678"

    def test_long_account_unchanged(self) -> None:
        assert normalize_account_number("1234567890") == "1234567890"

    def test_empty_or_none(self) -> None:
        assert normalize_account_number("") == ""
        assert normalize_account_number(None) == ""


class TestValidateBacs:
    def test_format_passes_returns_warning(self) -> None:
        ok, reason = validate_bacs("60-83-71", "12345678")
        # Phase 3 ships format-only validation; the modulus check is
        # absent, so format-passed cases come back as a warning the
        # caller surfaces to QA.
        assert ok is False
        assert reason == REASON_PASSED_FORMAT_ONLY

    def test_short_account_pads_then_passes_format(self) -> None:
        ok, reason = validate_bacs("60-83-71", "123456")
        assert ok is False
        assert reason == REASON_PASSED_FORMAT_ONLY

    def test_sort_code_too_short_fails(self) -> None:
        ok, reason = validate_bacs("12345", "12345678")
        assert ok is False
        assert reason == REASON_INVALID_FORMAT

    def test_sort_code_too_long_fails(self) -> None:
        ok, reason = validate_bacs("1234567", "12345678")
        assert ok is False
        assert reason == REASON_INVALID_FORMAT

    def test_account_short_input_pads_to_valid(self) -> None:
        """Short account numbers (5 digits) pad to 8 and pass format."""
        ok, reason = validate_bacs("608371", "12345")
        assert ok is False
        # Pads to "00012345" (8 digits) → format OK → warning, not error.
        assert reason == REASON_PASSED_FORMAT_ONLY

    def test_account_too_long_fails(self) -> None:
        ok, reason = validate_bacs("608371", "12345678901")
        assert ok is False
        assert reason == REASON_INVALID_FORMAT

    def test_non_digit_input_fails(self) -> None:
        # Entire input is letters → normalises to empty → fails format.
        ok, reason = validate_bacs("ABC", "XYZ")
        assert ok is False
        assert reason == REASON_INVALID_FORMAT

    def test_none_inputs_fail(self) -> None:
        ok, reason = validate_bacs(None, None)
        assert ok is False
        assert reason == REASON_INVALID_FORMAT
