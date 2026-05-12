"""QR code decoding service for scanned donation forms.

Each warm donation form carries a printed QR code encoding three fields:

    appeal_code|package_code|urn

These map directly to Campaign.appeal_code, PackageCode.code, and the donor's
URN respectively.  Cold records carry no QR sticker; warm records with a
damaged/illegible QR must be flagged for rescan rather than silently treated
as cold.

Usage::

    from scans.qr import QRResultKind, QRService

    result = QRService.decode(image_bytes)
    if result.kind is QRResultKind.DECODED:
        # warm record — appeal_code, package_code, urn are known
        print(result.appeal_code, result.package_code, result.urn)
    elif result.kind is QRResultKind.ABSENT:
        # cold record — fall back to folder-based campaign + OCR URN scan
        ...
    else:  # QRResultKind.MALFORMED
        # warm record with corrupt QR — flag for rescan
        print(result.error)
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

# Expected number of pipe-delimited segments in the QR payload.
_QR_SEGMENT_COUNT = 3
_SEPARATOR = "|"


class QRResultKind(StrEnum):
    """Discriminator for :class:`QRResult`."""

    ABSENT = "absent"
    DECODED = "decoded"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class QRResult:
    """Discriminated result returned by :meth:`QRService.decode`.

    Attributes:
        kind: Whether a QR was absent, decoded successfully, or malformed.
        appeal_code: Decoded appeal code (only set when ``kind`` is DECODED).
        package_code: Decoded package code (only set when ``kind`` is DECODED).
        urn: Decoded donor URN (only set when ``kind`` is DECODED).
        error: Human-readable failure reason (only set when ``kind`` is MALFORMED).
    """

    kind: QRResultKind
    appeal_code: str = ""
    package_code: str = ""
    urn: str = ""
    error: str = ""

    def to_payload_dict(self) -> dict[str, str]:
        """Return a dict view of the decoded payload (DECODED only)."""
        return {
            "appeal_code": self.appeal_code,
            "package_code": self.package_code,
            "urn": self.urn,
        }


class QRService:
    """Decode and parse QR codes from scanned donation form images.

    All methods are static — no instance state required.
    Requires pyzbar (``pip install pyzbar``) and Pillow, plus the
    ``libzbar0`` system library (already present in the Docker image).
    """

    @staticmethod
    def decode(image_bytes: bytes) -> QRResult:
        """Attempt to decode a QR code from raw image bytes.

        Returns a :class:`QRResult` whose ``kind`` distinguishes three cases:

        * ``QRResultKind.ABSENT`` — pyzbar found no QR code on the page (the
          form is a cold record, or the image is a PDF / unreadable bytes).
        * ``QRResultKind.DECODED`` — a QR was found and its payload parsed
          into ``appeal_code|package_code|urn`` with the required segments.
        * ``QRResultKind.MALFORMED`` — a QR was detected but its payload did
          not match the expected format (wrong segment count, missing
          ``appeal_code`` or ``urn``, or the decoder raised). The form is a
          warm record with a damaged sticker and must be flagged for rescan.

        Args:
            image_bytes: Raw image bytes (JPEG, PNG, TIFF).

        Returns:
            A :class:`QRResult` describing the outcome.
        """
        try:
            return QRService._decode_internal(image_bytes)
        except Exception as exc:
            logger.exception("Unexpected error during QR decode")
            return QRResult(
                kind=QRResultKind.MALFORMED,
                error=f"QR decoder raised: {exc}",
            )

    @staticmethod
    def _is_pdf(image_bytes: bytes) -> bool:
        """Return True if the bytes appear to be a PDF."""
        return image_bytes[:4] == b"%PDF"

    @staticmethod
    def _decode_internal(image_bytes: bytes) -> QRResult:
        """Internal QR decode implementation."""
        from io import BytesIO

        from PIL import Image
        from pyzbar import pyzbar

        # PDFs are not directly decodable as images — treat as no QR present.
        if QRService._is_pdf(image_bytes):
            return QRResult(kind=QRResultKind.ABSENT)

        image = Image.open(BytesIO(image_bytes))

        # Convert to RGB if needed (pyzbar works best on RGB/grayscale)
        if image.mode not in ("RGB", "L", "RGBA"):
            image = image.convert("RGB")

        decoded_objects = pyzbar.decode(image)
        qr_objects = [obj for obj in decoded_objects if obj.type == "QRCODE"]
        if not qr_objects:
            return QRResult(kind=QRResultKind.ABSENT)

        last_error = ""
        for obj in qr_objects:
            raw = obj.data.decode("utf-8", errors="replace").strip()
            parsed = QRService._parse_payload(raw)
            if parsed is not None:
                logger.debug(
                    "QR decoded: appeal_code=%r package_code=%r urn=%r",
                    parsed["appeal_code"],
                    parsed["package_code"],
                    parsed["urn"],
                )
                return QRResult(
                    kind=QRResultKind.DECODED,
                    appeal_code=parsed["appeal_code"],
                    package_code=parsed["package_code"],
                    urn=parsed["urn"],
                )

            last_error = f"QR payload format unrecognised — raw={raw!r}"
            logger.warning(
                "QR code found but payload format unrecognised — raw=%r", raw
            )

        return QRResult(kind=QRResultKind.MALFORMED, error=last_error)

    @staticmethod
    def _parse_payload(raw: str) -> dict[str, str] | None:
        """Parse a pipe-delimited QR payload string.

        Expected format::

            appeal_code|package_code|urn

        Args:
            raw: Raw decoded QR string.

        Returns:
            Parsed dict or ``None`` if the format is invalid.
        """
        parts = raw.split(_SEPARATOR)
        if len(parts) != _QR_SEGMENT_COUNT:
            return None

        appeal_code, package_code, urn = (p.strip() for p in parts)

        # All three segments must be non-empty for a valid warm record.
        if not appeal_code or not urn:
            logger.warning(
                "QR payload missing required segments: appeal_code=%r urn=%r",
                appeal_code,
                urn,
            )
            return None

        return {
            "appeal_code": appeal_code,
            "package_code": package_code,
            "urn": urn,
        }

    @staticmethod
    def encode_payload(appeal_code: str, package_code: str, urn: str) -> str:
        """Build the canonical QR payload string for a warm record.

        Useful for generating QR codes to print on donation forms.

        Args:
            appeal_code: Campaign appeal code.
            package_code: Package code (may be empty for house-file campaigns).
            urn: Donor URN.

        Returns:
            Pipe-delimited string ready to encode into a QR code.
        """
        return _SEPARATOR.join([appeal_code, package_code, urn])
