"""Unit tests for QRService, _detect_mime_type, and ScanProcessingService._decode_qr_from_scan.

Covers:
- QRService._parse_payload — pipe-delimited QR payload parsing
- QRService.encode_payload — round-trip encoding
- QRService.decode — discriminated QRResult with absent/decoded/malformed kinds
- _detect_mime_type — magic-byte MIME detection
- ScanProcessingService._decode_qr_from_scan — placeholder mutation on warm/cold records
"""

import io
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import django

django.setup()

from scans.document_ai import _detect_mime_type  # noqa: E402
from scans.qr import QRResult, QRResultKind, QRService  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_qr_image(payload: str) -> bytes:
    """Generate a real QR-code PNG image encoding ``payload``."""
    import qrcode  # type: ignore[import-untyped]

    buf = io.BytesIO()
    img = qrcode.make(payload)
    img.save(buf, format="PNG")  # type: ignore[call-arg]
    return buf.getvalue()


def _blank_png(width: int = 200, height: int = 200) -> bytes:
    """Return a plain white PNG image with no QR code."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="white").save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# QRService — _parse_payload
# ─────────────────────────────────────────────────────────────────────────────


class TestQRServiceParsePayload:
    """Tests for QRService._parse_payload."""

    def test_valid_payload_returns_dict(self) -> None:
        result = QRService._parse_payload("APP001|PKG01|URN12345")
        assert result == {
            "appeal_code": "APP001",
            "package_code": "PKG01",
            "urn": "URN12345",
        }

    def test_package_code_may_be_empty(self) -> None:
        """House-file campaigns have no package code — empty middle segment is valid."""
        result = QRService._parse_payload("APP001||URN12345")
        assert result is not None
        assert result["appeal_code"] == "APP001"
        assert result["package_code"] == ""
        assert result["urn"] == "URN12345"

    def test_two_segments_returns_none(self) -> None:
        assert QRService._parse_payload("APP001|PKG01") is None

    def test_four_segments_returns_none(self) -> None:
        assert QRService._parse_payload("A|B|C|D") is None

    def test_empty_appeal_code_returns_none(self) -> None:
        assert QRService._parse_payload("|PKG01|URN12345") is None

    def test_empty_urn_returns_none(self) -> None:
        assert QRService._parse_payload("APP001|PKG01|") is None

    def test_whitespace_is_stripped(self) -> None:
        result = QRService._parse_payload(" APP001 | PKG01 | URN12345 ")
        assert result is not None
        assert result["appeal_code"] == "APP001"
        assert result["urn"] == "URN12345"

    def test_empty_string_returns_none(self) -> None:
        assert QRService._parse_payload("") is None


# ─────────────────────────────────────────────────────────────────────────────
# QRService — encode_payload
# ─────────────────────────────────────────────────────────────────────────────


class TestQRServiceEncodePayload:
    """Tests for QRService.encode_payload."""

    def test_roundtrip(self) -> None:
        payload = QRService.encode_payload("APP001", "PKG01", "URN12345")
        parsed = QRService._parse_payload(payload)
        assert parsed == {
            "appeal_code": "APP001",
            "package_code": "PKG01",
            "urn": "URN12345",
        }

    def test_produces_pipe_delimited_string(self) -> None:
        assert QRService.encode_payload("A", "B", "C") == "A|B|C"

    def test_empty_package_code_allowed(self) -> None:
        assert QRService.encode_payload("APP001", "", "URN12345") == "APP001||URN12345"


# ─────────────────────────────────────────────────────────────────────────────
# QRService — decode  (real pyzbar + real QR images)
# ─────────────────────────────────────────────────────────────────────────────


class TestQRServiceDecode:
    """Integration-style tests using real pyzbar decoding on generated QR images."""

    def test_decode_warm_record(self) -> None:
        result = QRService.decode(_make_qr_image("APP001|PKG01|URN12345"))
        assert isinstance(result, QRResult)
        assert result.kind is QRResultKind.DECODED
        assert result.appeal_code == "APP001"
        assert result.package_code == "PKG01"
        assert result.urn == "URN12345"
        assert result.error == ""

    def test_decode_no_qr_returns_absent(self) -> None:
        """Blank white image has no QR code — must return ABSENT."""
        result = QRService.decode(_blank_png())
        assert result.kind is QRResultKind.ABSENT
        assert result.urn == ""

    def test_decode_malformed_payload_returns_malformed(self) -> None:
        """A QR encoding non-pipe-delimited data must return MALFORMED with error."""
        result = QRService.decode(_make_qr_image("this-is-not-valid"))
        assert result.kind is QRResultKind.MALFORMED
        assert result.error != ""
        assert "this-is-not-valid" in result.error

    def test_decode_bad_bytes_returns_absent(self) -> None:
        """Arbitrary garbage bytes raise inside Pillow — surfaced as MALFORMED."""
        result = QRService.decode(b"\x00\x01\x02garbage")
        assert result.kind is QRResultKind.MALFORMED
        assert result.error != ""

    def test_decode_empty_package_code_warm_record(self) -> None:
        """House-file records (no package) must decode as warm with package_code=''."""
        result = QRService.decode(_make_qr_image("APP001||URN99999"))
        assert result.kind is QRResultKind.DECODED
        assert result.package_code == ""
        assert result.urn == "URN99999"

    def test_decode_two_segment_payload_returns_malformed(self) -> None:
        """Wrong segment count must surface as MALFORMED with an error message."""
        result = QRService.decode(_make_qr_image("only_one_segment"))
        assert result.kind is QRResultKind.MALFORMED
        assert "only_one_segment" in result.error

    def test_decode_decoder_exception_returns_malformed(self) -> None:
        """A pyzbar/Pillow exception must be captured as MALFORMED, not raised."""
        with patch("PIL.Image.open", side_effect=RuntimeError("boom")):
            result = QRService.decode(_make_qr_image("APP001|PKG01|URN12345"))
        assert result.kind is QRResultKind.MALFORMED
        assert "boom" in result.error

    def test_decoded_result_to_payload_dict_roundtrip(self) -> None:
        """to_payload_dict() must mirror the legacy dict shape used by extractors."""
        result = QRResult(
            kind=QRResultKind.DECODED,
            appeal_code="APP001",
            package_code="PKG01",
            urn="URN12345",
        )
        assert result.to_payload_dict() == {
            "appeal_code": "APP001",
            "package_code": "PKG01",
            "urn": "URN12345",
        }


# ─────────────────────────────────────────────────────────────────────────────
# _detect_mime_type
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectMimeType:
    """Tests for document_ai_service._detect_mime_type (magic-byte detection)."""

    def test_jpeg(self) -> None:
        assert _detect_mime_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"

    def test_png(self) -> None:
        assert _detect_mime_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"

    def test_tiff_little_endian(self) -> None:
        assert _detect_mime_type(b"II*\x00rest") == "image/tiff"

    def test_tiff_big_endian(self) -> None:
        assert _detect_mime_type(b"MM\x00*rest") == "image/tiff"

    def test_pdf(self) -> None:
        assert _detect_mime_type(b"%PDFrest") == "application/pdf"

    def test_unknown_defaults_to_jpeg(self) -> None:
        assert _detect_mime_type(b"\x00\x00unknown") == "image/jpeg"


# ─────────────────────────────────────────────────────────────────────────────
# ScanProcessingService — _decode_qr_from_scan
# ─────────────────────────────────────────────────────────────────────────────


class TestDecodeQrFromScan:
    """Tests for ScanProcessingService._decode_qr_from_scan."""

    def _placeholder(self) -> Any:
        """Build a minimal placeholder-like SimpleNamespace."""
        ph = SimpleNamespace()
        ph.id = uuid.uuid4()
        ph.qr_decoded = False
        ph.qr_raw = ""
        ph.ocr_data = {}
        return ph

    def test_warm_record_sets_qr_decoded_true(self) -> None:
        from scans.scan_processing import ScanProcessingService

        image_bytes = _make_qr_image("APP001|PKG01|URN12345")
        placeholder = self._placeholder()

        result = ScanProcessingService._decode_qr_from_scan(placeholder, image_bytes)

        assert result is not None
        assert placeholder.qr_decoded is True
        assert placeholder.qr_raw == "APP001|PKG01|URN12345"
        assert placeholder.ocr_data["qr_kind"] == "decoded"

    def test_cold_record_sets_qr_decoded_false(self) -> None:
        from scans.scan_processing import ScanProcessingService

        placeholder = self._placeholder()
        result = ScanProcessingService._decode_qr_from_scan(
            placeholder, _blank_png(100, 100)
        )

        assert result is None
        assert placeholder.qr_decoded is False
        assert placeholder.qr_raw == ""
        assert placeholder.ocr_data["qr_kind"] == "absent"

    def test_qr_raw_matches_encode_payload(self) -> None:
        from scans.scan_processing import ScanProcessingService

        expected_raw = QRService.encode_payload("APP001", "PKG01", "URN12345")
        placeholder = self._placeholder()
        ScanProcessingService._decode_qr_from_scan(
            placeholder, _make_qr_image(expected_raw)
        )

        assert placeholder.qr_raw == expected_raw

    def test_malformed_qr_records_kind_and_error(self) -> None:
        """A QR with bad payload must surface as MALFORMED in placeholder.ocr_data."""
        from scans.scan_processing import ScanProcessingService

        placeholder = self._placeholder()
        result = ScanProcessingService._decode_qr_from_scan(
            placeholder, _make_qr_image("only_one_segment")
        )

        assert result is None
        assert placeholder.qr_decoded is False
        assert placeholder.ocr_data["qr_kind"] == "malformed"
        assert "only_one_segment" in placeholder.ocr_data["qr_error"]
