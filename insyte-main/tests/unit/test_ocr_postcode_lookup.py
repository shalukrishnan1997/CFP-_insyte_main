"""Unit tests for OCR postcode enrichment lookups."""

from unittest.mock import MagicMock, patch

from scans.ocr.postcode_lookup import lookup_postcode


class TestLookupPostcode:
    """Verify OCR postcode lookups use the fixed-host HTTPS client safely."""

    def setup_method(self) -> None:
        lookup_postcode.cache_clear()

    def test_successful_lookup_returns_city_and_county(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        response.status = 200
        response.read.return_value = b'{"result":{"admin_district":"City of Westminster","admin_county":"London"}}'
        connection = MagicMock()
        connection.getresponse.return_value = response

        with patch(
            "scans.ocr.postcode_lookup.HTTPSConnection",
            return_value=connection,
        ) as connection_cls:
            result = lookup_postcode("SW1A 1AA")

        assert result == {"city": "City Of Westminster", "county": "London"}
        connection_cls.assert_called_once_with("api.postcodes.io", timeout=3)
        connection.request.assert_called_once_with("GET", "/postcodes/SW1A1AA")
        connection.close.assert_called_once()

    def test_not_found_returns_empty_dict(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        response.status = 404
        connection = MagicMock()
        connection.getresponse.return_value = response

        with patch(
            "scans.ocr.postcode_lookup.HTTPSConnection",
            return_value=connection,
        ):
            result = lookup_postcode("ZZ99 9ZZ")

        assert result == {}
        connection.close.assert_called_once()

    def test_non_200_returns_none(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        response.status = 503
        connection = MagicMock()
        connection.getresponse.return_value = response

        with patch(
            "scans.ocr.postcode_lookup.HTTPSConnection",
            return_value=connection,
        ):
            result = lookup_postcode("SW1A 1AA")

        assert result is None
        connection.close.assert_called_once()

    def test_connection_error_returns_none(self) -> None:
        connection = MagicMock()
        connection.request.side_effect = OSError("network down")

        with patch(
            "scans.ocr.postcode_lookup.HTTPSConnection",
            return_value=connection,
        ):
            result = lookup_postcode("SW1A 1AA")

        assert result is None
        connection.close.assert_called_once()
