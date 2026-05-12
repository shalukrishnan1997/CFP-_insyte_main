"""Server-side blackout renderer for scan redaction.

The QA UI emits normalised (0..1) rectangle coordinates over a scan; this
module rasterises the source (image or PDF page 0), bakes opaque black
rectangles on top, and re-encodes the result as PNG. PNG is used as the
single output format so the QA viewer can display redacted scans without
needing a content-type round-trip.

This module is pure: no Django models, no R2 calls, no I/O beyond the
in-memory bytes provided by the caller.
"""

import io
import logging

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

_PDF_RENDER_SCALE = 2.5
_PDF_CONTENT_TYPE = "application/pdf"
_OUTPUT_CONTENT_TYPE = "image/png"
_BOUNDS_TOLERANCE = 1.0001


def _validate_rect(rect: dict[str, float]) -> tuple[float, float, float, float]:
    """Validate one normalised rect and return (x, y, width, height)."""
    try:
        x = float(rect["x"])
        y = float(rect["y"])
        width = float(rect["width"])
        height = float(rect["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid rect: {rect!r}") from exc

    if width <= 0 or height <= 0:
        raise ValueError(f"Zero-area rect: {rect!r}")
    if x < 0 or y < 0 or width > 1 or height > 1:
        raise ValueError(f"Rect coordinate out of bounds (0..1): {rect!r}")
    if x + width > _BOUNDS_TOLERANCE or y + height > _BOUNDS_TOLERANCE:
        raise ValueError(f"Rect extends past image bounds: {rect!r}")
    return x, y, width, height


def _load_pdf_page_zero(source_bytes: bytes) -> Image.Image:
    """Render page 0 of *source_bytes* (a PDF) to a PIL image."""
    document = pdfium.PdfDocument(source_bytes)
    try:
        if len(document) == 0:
            raise ValueError("PDF has no pages")
        page = document[0]
        try:
            # pdfium's stub annotates scale as int but it accepts float at runtime.
            bitmap = page.render(scale=_PDF_RENDER_SCALE)  # pyright: ignore[reportArgumentType]
            try:
                return bitmap.to_pil()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


def apply_redactions(
    source_bytes: bytes,
    source_content_type: str,
    rects: list[dict[str, float]],
) -> tuple[bytes, str]:
    """Apply blackout rectangles to a scan and return PNG bytes.

    Args:
        source_bytes: The original image or PDF bytes from R2.
        source_content_type: MIME type ("image/png", "image/jpeg",
            "image/tiff", "application/pdf", ...).
        rects: List of normalized 0..1 rectangles. Each dict has keys
            x, y, width, height.

    Returns:
        (redacted_png_bytes, "image/png"). Output is always PNG so the
        QA viewer can display it without a content-type round-trip.

    Raises:
        ValueError: rect coordinates out of bounds, zero-area rect, or
            unsupported content type.
    """
    # Validate up front so a bad rect rejects the whole request before
    # we spend any cycles decoding the source.
    validated = [_validate_rect(rect) for rect in rects]

    content_type = (source_content_type or "").lower().strip()
    if content_type == _PDF_CONTENT_TYPE:
        image = _load_pdf_page_zero(source_bytes)
    elif content_type.startswith("image/"):
        image = Image.open(io.BytesIO(source_bytes))
        image.load()
    else:
        raise ValueError(f"Unsupported content type: {source_content_type!r}")

    if image.mode != "RGB":
        image = image.convert("RGB")

    if validated:
        width_px, height_px = image.size
        draw = ImageDraw.Draw(image)
        for x, y, w, h in validated:
            x0 = int(x * width_px)
            y0 = int(y * height_px)
            x1 = min(width_px, int((x + w) * width_px))
            y1 = min(height_px, int((y + h) * height_px))
            draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), _OUTPUT_CONTENT_TYPE
