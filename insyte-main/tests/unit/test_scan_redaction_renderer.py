"""Unit tests for scans.scan_redaction_renderer.apply_redactions."""

import io

import pypdfium2 as pdfium
import pytest
from PIL import Image

from scans.scan_redaction_renderer import apply_redactions


def _make_solid_png(
    width: int, height: int, color: tuple[int, int, int] = (255, 255, 255)
) -> bytes:
    """Return PNG bytes for a solid-colour image."""
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _make_solid_jpeg(
    width: int, height: int, color: tuple[int, int, int] = (255, 255, 255)
) -> bytes:
    """Return JPEG bytes for a solid-colour image."""
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _make_blank_pdf(width_pt: int = 200, height_pt: int = 100) -> bytes:
    """Return bytes for a one-page blank PDF generated via pypdfium2."""
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(width_pt, height_pt)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_image_happy_path_paints_rect_black() -> None:
    source = _make_solid_png(100, 200)
    rects = [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}]

    out_bytes, out_ct = apply_redactions(source, "image/png", rects)

    assert out_ct == "image/png"
    result = Image.open(io.BytesIO(out_bytes))
    assert result.format == "PNG"
    result_rgb = result.convert("RGB")

    # Inside the rect (e.g. centre of it) → black
    inside_x = int((0.1 + 0.3 / 2) * 100)
    inside_y = int((0.2 + 0.4 / 2) * 200)
    assert result_rgb.getpixel((inside_x, inside_y)) == (0, 0, 0)

    # Outside the rect → still white
    assert result_rgb.getpixel((0, 0)) == (255, 255, 255)
    assert result_rgb.getpixel((99, 199)) == (255, 255, 255)


def test_multiple_non_overlapping_rects_all_painted() -> None:
    source = _make_solid_png(200, 200)
    rects = [
        {"x": 0.0, "y": 0.0, "width": 0.2, "height": 0.2},
        {"x": 0.6, "y": 0.6, "width": 0.2, "height": 0.2},
    ]

    out_bytes, _ = apply_redactions(source, "image/png", rects)
    result = Image.open(io.BytesIO(out_bytes)).convert("RGB")

    # Centre of first rect
    assert result.getpixel((20, 20)) == (0, 0, 0)
    # Centre of second rect
    assert result.getpixel((140, 140)) == (0, 0, 0)
    # Between them — untouched white
    assert result.getpixel((100, 100)) == (255, 255, 255)


def test_empty_rects_returns_unchanged_pixels_as_png() -> None:
    source = _make_solid_png(50, 50)

    out_bytes, out_ct = apply_redactions(source, "image/png", [])

    assert out_ct == "image/png"
    result = Image.open(io.BytesIO(out_bytes)).convert("RGB")
    assert result.size == (50, 50)
    # All pixels should still be white
    for x in (0, 25, 49):
        for y in (0, 25, 49):
            assert result.getpixel((x, y)) == (255, 255, 255)


def test_pdf_happy_path_renders_page_zero_and_returns_png() -> None:
    source = _make_blank_pdf()
    rects = [{"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5}]

    out_bytes, out_ct = apply_redactions(source, "application/pdf", rects)

    assert out_ct == "image/png"
    result = Image.open(io.BytesIO(out_bytes))
    assert result.format == "PNG"
    result_rgb = result.convert("RGB")
    # Centre pixel falls inside the rect → black
    cx = result_rgb.size[0] // 2
    cy = result_rgb.size[1] // 2
    assert result_rgb.getpixel((cx, cy)) == (0, 0, 0)


def test_jpeg_input_returns_png_output() -> None:
    source = _make_solid_jpeg(80, 80)
    rects = [{"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5}]

    out_bytes, out_ct = apply_redactions(source, "image/jpeg", rects)

    assert out_ct == "image/png"
    result = Image.open(io.BytesIO(out_bytes))
    assert result.format == "PNG"
    result_rgb = result.convert("RGB")
    assert result_rgb.getpixel((10, 10)) == (0, 0, 0)
    # JPEG is lossy so the "white" corner may not be exact; just check
    # we did NOT blackout outside the rect.
    corner = result_rgb.getpixel((70, 70))
    assert isinstance(corner, tuple)
    r, g, b = corner[:3]
    assert r > 200 and g > 200 and b > 200


def test_out_of_bounds_rect_raises_value_error() -> None:
    source = _make_solid_png(50, 50)
    rects = [{"x": 1.1, "y": 0.0, "width": 0.1, "height": 0.1}]

    with pytest.raises(ValueError):
        apply_redactions(source, "image/png", rects)


def test_rect_extending_past_right_edge_raises_value_error() -> None:
    source = _make_solid_png(50, 50)
    rects = [{"x": 0.8, "y": 0.0, "width": 0.5, "height": 0.1}]

    with pytest.raises(ValueError):
        apply_redactions(source, "image/png", rects)


def test_zero_width_rect_raises_value_error() -> None:
    source = _make_solid_png(50, 50)
    rects = [{"x": 0.1, "y": 0.1, "width": 0.0, "height": 0.5}]

    with pytest.raises(ValueError):
        apply_redactions(source, "image/png", rects)


def test_zero_height_rect_raises_value_error() -> None:
    source = _make_solid_png(50, 50)
    rects = [{"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.0}]

    with pytest.raises(ValueError):
        apply_redactions(source, "image/png", rects)


def test_unsupported_content_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported content type"):
        apply_redactions(b"hello", "text/plain", [])


def test_missing_rect_key_raises_value_error() -> None:
    source = _make_solid_png(50, 50)
    with pytest.raises(ValueError):
        apply_redactions(source, "image/png", [{"x": 0.1, "y": 0.1, "width": 0.2}])
