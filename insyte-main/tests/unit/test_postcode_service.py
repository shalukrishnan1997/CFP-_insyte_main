"""Unit tests for core.services.postcode — PostcodesIOService."""

from typing import Any
from unittest.mock import MagicMock, patch

from core.services.postcode import (
    PostcodesIOService,
    _is_valid_format,
    _lookup_postcode,
    _normalise_postcode,
)


class TestNormalisePostcode:
    """Tests for _normalise_postcode helper."""

    def test_sw1a_1aa(self) -> None:
        assert _normalise_postcode("SW1A1AA") == "SW1A 1AA"

    def test_ec1a_1bb(self) -> None:
        assert _normalise_postcode("EC1A1BB") == "EC1A 1BB"

    def test_already_spaced(self) -> None:
        assert _normalise_postcode("AL10 8DG") == "AL10 8DG"

    def test_lowercase_normalised(self) -> None:
        result = _normalise_postcode("sw1a 1aa")
        assert result == "SW1A 1AA"

    def test_trailing_whitespace_stripped(self) -> None:
        result = _normalise_postcode("  AL10 8DG  ")
        assert result == "AL10 8DG"

    def test_four_char_outward(self) -> None:
        # e.g. "AL10 8DG" — outward = "AL10", inward = "8DG"
        assert _normalise_postcode("AL108DG") == "AL10 8DG"

    def test_short_postcode_returns_original_uppercased(self) -> None:
        # Less than 5 chars → can't split cleanly, return stripped+upper
        result = _normalise_postcode("EC1")
        assert result == "EC1"


class TestIsValidFormat:
    """Tests for _is_valid_format helper."""

    def test_valid_sw1a(self) -> None:
        assert _is_valid_format("SW1A 1AA") is True

    def test_valid_ec1a(self) -> None:
        assert _is_valid_format("EC1A 1BB") is True

    def test_valid_simple(self) -> None:
        assert _is_valid_format("WC2N 5DU") is True

    def test_invalid_format(self) -> None:
        assert _is_valid_format("12345") is False

    def test_too_short(self) -> None:
        assert _is_valid_format("AB1") is False

    def test_empty_string(self) -> None:
        assert _is_valid_format("") is False

    def test_non_uk_format(self) -> None:
        assert _is_valid_format("10001") is False

    def test_unspaced_valid(self) -> None:
        assert _is_valid_format("SW1A1AA") is True


class TestLookupPostcode:
    """Tests for _lookup_postcode cached function."""

    def test_successful_lookup_returns_result(self) -> None:
        mock_result = {
            "postcode": "AL10 8DG",
            "parish": "Hatfield",
            "admin_district": "Welwyn Hatfield",
            "admin_county": "Hertfordshire",
            "country": "England",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 200, "result": mock_result}

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            # Clear the LRU cache to ensure fresh call
            _lookup_postcode.cache_clear()
            result = _lookup_postcode("AL10 8DG")

        assert result == mock_result

    def test_404_returns_none(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            result = _lookup_postcode("ZZ99 9ZZ")

        assert result is None

    def test_non_200_non_404_returns_none(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            result = _lookup_postcode("AL10 8DG")

        assert result is None

    def test_request_exception_returns_none(self) -> None:
        import requests as req_lib

        with patch(
            "core.services.postcode.requests.get",
            side_effect=req_lib.RequestException("timeout"),
        ):
            _lookup_postcode.cache_clear()
            result = _lookup_postcode("AL10 8DG")

        assert result is None


class TestPostcodesIOServiceEnrichAddress:
    """Tests for PostcodesIOService.enrich_address."""

    def test_empty_postcode_is_noop(self) -> None:
        result: dict[str, Any] = {"postcode": "", "city": ""}
        PostcodesIOService.enrich_address(result)
        assert result.get("postcode_valid") is None  # untouched

    def test_none_postcode_is_noop(self) -> None:
        result: dict[str, Any] = {"city": "London"}
        PostcodesIOService.enrich_address(result)
        assert "postcode_valid" not in result

    def test_invalid_format_marks_invalid(self) -> None:
        result: dict[str, Any] = {"postcode": "NOTAPOSTCODE"}
        PostcodesIOService.enrich_address(result)
        assert result["postcode_valid"] is False

    def test_valid_postcode_normalised_in_result(self) -> None:
        api_data = {
            "parish": "Hatfield",
            "admin_district": "Welwyn Hatfield",
            "admin_county": "Hertfordshire",
            "country": "England",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 200, "result": api_data}

        result: dict[str, Any] = {"postcode": "AL108DG", "city": "", "county": ""}

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            PostcodesIOService.enrich_address(result)

        assert result["postcode"] == "AL10 8DG"
        assert result["postcode_valid"] is True

    def test_city_enriched_from_parish(self) -> None:
        api_data = {
            "parish": "Hatfield",
            "admin_district": "Welwyn Hatfield",
            "admin_county": "Hertfordshire",
            "country": "England",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 200, "result": api_data}

        result: dict[str, Any] = {"postcode": "AL10 8DG", "city": "", "county": ""}

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            PostcodesIOService.enrich_address(result)

        assert result["city"] == "Hatfield"
        assert result.get("city_confidence") == 0.95

    def test_city_falls_back_to_admin_district_when_no_parish(self) -> None:
        api_data = {
            "parish": None,
            "admin_district": "Glasgow City",
            "admin_county": None,
            "country": "Scotland",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 200, "result": api_data}

        result: dict[str, Any] = {"postcode": "G1 1AA", "city": "", "county": ""}

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            PostcodesIOService.enrich_address(result)

        assert result["city"] == "Glasgow City"

    def test_county_enriched_from_admin_county(self) -> None:
        api_data = {
            "parish": "Hatfield",
            "admin_district": "Welwyn Hatfield",
            "admin_county": "Hertfordshire",
            "country": "England",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 200, "result": api_data}

        result: dict[str, Any] = {
            "postcode": "AL10 8DG",
            "city": "Hatfield",
            "county": "",
        }

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            PostcodesIOService.enrich_address(result)

        assert result["county"] == "Hertfordshire"

    def test_county_uses_admin_district_for_scotland(self) -> None:
        api_data = {
            "parish": None,
            "admin_district": "Glasgow City",
            "admin_county": None,
            "country": "Scotland",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 200, "result": api_data}

        result: dict[str, Any] = {
            "postcode": "G1 1AA",
            "city": "Glasgow City",
            "county": "",
        }

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            PostcodesIOService.enrich_address(result)

        assert result["county"] == "Glasgow City"

    def test_existing_city_not_overwritten(self) -> None:
        api_data = {
            "parish": "Hatfield",
            "admin_district": "Welwyn Hatfield",
            "admin_county": "Hertfordshire",
            "country": "England",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 200, "result": api_data}

        result: dict[str, Any] = {
            "postcode": "AL10 8DG",
            "city": "Existing City",
            "county": "",
        }

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            PostcodesIOService.enrich_address(result)

        assert result["city"] == "Existing City"

    def test_api_failure_marks_invalid(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 404

        result: dict[str, Any] = {"postcode": "AL10 8DG", "city": "", "county": ""}

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            PostcodesIOService.enrich_address(result)

        assert result["postcode_valid"] is False

    def test_existing_county_not_overwritten(self) -> None:
        api_data = {
            "parish": "Hatfield",
            "admin_district": "Welwyn Hatfield",
            "admin_county": "Hertfordshire",
            "country": "England",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 200, "result": api_data}

        result: dict[str, Any] = {
            "postcode": "AL10 8DG",
            "city": "",
            "county": "Existing County",
        }

        with patch("core.services.postcode.requests.get", return_value=mock_response):
            _lookup_postcode.cache_clear()
            PostcodesIOService.enrich_address(result)

        assert result["county"] == "Existing County"
