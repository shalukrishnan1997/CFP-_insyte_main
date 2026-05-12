"""Cache-Control behaviour for ``scan_image_serve`` responses.

Pending-redaction scans must never be retained by the browser cache, even
on disk. This module covers each response branch (PDF page extract, redirect
fallback, and presigned redirect) for both pending and completed placeholders.
"""

from io import BytesIO
from unittest.mock import MagicMock, patch

import django
import pytest
from django.test import RequestFactory
from django.urls import reverse
from pypdf import PdfWriter

django.setup()

from scans import api_views as scan_api_views  # noqa: E402
from scans.models import ScanPlaceholder  # noqa: E402
from tests.factories import ScanPlaceholderFactory, UserFactory  # noqa: E402


def _build_pdf_bytes() -> bytes:
    """Return bytes for a minimal one-page PDF."""
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buffer)
    return buffer.getvalue()


def _build_request(
    *,
    key: str,
    placeholder_id: str,
    user: object,
    allow_pending_redaction: bool = False,
    inline: bool = False,
) -> object:
    """Construct a GET request to the scan-image-serve endpoint."""
    params: dict[str, str] = {
        "key": key,
        "id": placeholder_id,
        "token": scan_api_views.build_scan_view_token(placeholder_id),
    }
    if allow_pending_redaction:
        params["allow_pending_redaction"] = "1"
    if inline:
        params["inline"] = "1"
    request = RequestFactory().get(reverse("custom_admin:scan_image_serve"), params)
    request.user = user  # pyright: ignore[reportAttributeAccessIssue]
    return request


@pytest.mark.django_db
def test_pdf_page_response_for_unredacted_placeholder_sets_no_store(
    redaction_required_all: object,
) -> None:
    """Pending-redaction PDF page responses must opt out of the browser cache."""
    del redaction_required_all

    staff_user = UserFactory(is_staff=True)
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/pending.pdf",
        page_keys=["ScanOutput/demo/pending.pdf"],
        redaction_status=ScanPlaceholder.REDACTION_PENDING,
    )

    virtual_key = "ScanOutput/demo/pending.pdf::pdf_page::1"
    request = _build_request(
        key=virtual_key,
        placeholder_id=str(placeholder.id),
        user=staff_user,
        allow_pending_redaction=True,
    )

    mock_r2_client = MagicMock()
    mock_r2_client.get_object.return_value = {"Body": BytesIO(_build_pdf_bytes())}

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.get_r2_client", return_value=mock_r2_client),
    ):
        response = scan_api_views.scan_image_serve(request)  # pyright: ignore[reportArgumentType]

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_pdf_page_response_for_completed_placeholder_keeps_private_cache() -> None:
    """Completed-redaction scans keep the original private cache header."""
    staff_user = UserFactory(is_staff=True)
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/done.pdf",
        page_keys=["ScanOutput/demo/done.pdf"],
        redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
    )

    virtual_key = "ScanOutput/demo/done.pdf::pdf_page::1"
    request = _build_request(
        key=virtual_key,
        placeholder_id=str(placeholder.id),
        user=staff_user,
    )

    mock_r2_client = MagicMock()
    mock_r2_client.get_object.return_value = {"Body": BytesIO(_build_pdf_bytes())}

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.get_r2_client", return_value=mock_r2_client),
    ):
        response = scan_api_views.scan_image_serve(request)  # pyright: ignore[reportArgumentType]

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, max-age=3600"


@pytest.mark.django_db
def test_redirect_for_unredacted_placeholder_sets_no_store(
    redaction_required_all: object,
) -> None:
    """The presigned-URL redirect must mark unredacted scans as no-store."""
    del redaction_required_all

    staff_user = UserFactory(is_staff=True)
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/pending_redirect.png",
        page_keys=["ScanOutput/demo/pending_redirect.png"],
        redaction_status=ScanPlaceholder.REDACTION_PENDING,
    )

    request = _build_request(
        key="ScanOutput/demo/pending_redirect.png",
        placeholder_id=str(placeholder.id),
        user=staff_user,
        allow_pending_redaction=True,
    )

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch(
            "core.storage_backends.r2_presigned_url",
            return_value="https://r2.example.com/signed",
        ),
    ):
        response = scan_api_views.scan_image_serve(request)  # pyright: ignore[reportArgumentType]

    assert response.status_code == 302
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_redirect_for_completed_placeholder_does_not_set_no_store() -> None:
    """Redirects for completed scans must not be marked no-store."""
    staff_user = UserFactory(is_staff=True)
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/done_redirect.png",
        page_keys=["ScanOutput/demo/done_redirect.png"],
        redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
    )

    request = _build_request(
        key="ScanOutput/demo/done_redirect.png",
        placeholder_id=str(placeholder.id),
        user=staff_user,
    )

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch(
            "core.storage_backends.r2_presigned_url",
            return_value="https://r2.example.com/signed",
        ),
    ):
        response = scan_api_views.scan_image_serve(request)  # pyright: ignore[reportArgumentType]

    assert response.status_code == 302
    assert response.get("Cache-Control", "") != "no-store"


@pytest.mark.django_db
def test_inline_proxy_response_for_unredacted_placeholder_sets_no_store(
    redaction_required_all: object,
) -> None:
    """The inline-proxy bytes branch must also mark unredacted scans as no-store."""
    del redaction_required_all

    staff_user = UserFactory(is_staff=True)
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/inline_pending.png",
        page_keys=["ScanOutput/demo/inline_pending.png"],
        redaction_status=ScanPlaceholder.REDACTION_PENDING,
    )

    request = _build_request(
        key="ScanOutput/demo/inline_pending.png",
        placeholder_id=str(placeholder.id),
        user=staff_user,
        allow_pending_redaction=True,
        inline=True,
    )

    mock_r2_client = MagicMock()
    mock_r2_client.get_object.return_value = {
        "Body": BytesIO(b"image-bytes"),
        "ContentType": "image/png",
    }

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.get_r2_client", return_value=mock_r2_client),
    ):
        response = scan_api_views.scan_image_serve(request)  # pyright: ignore[reportArgumentType]

    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_unknown_placeholder_id_returns_404_when_redaction_not_required() -> None:
    """The hoisted lookup runs even when no payment method requires redaction."""
    import json
    import uuid

    staff_user = UserFactory(is_staff=True)
    bogus_id = str(uuid.uuid4())
    request = _build_request(
        key="ScanOutput/demo/anything.png",
        placeholder_id=bogus_id,
        user=staff_user,
    )

    response = scan_api_views.scan_image_serve(request)  # pyright: ignore[reportArgumentType]

    assert response.status_code == 404
    assert json.loads(response.content) == {"error": "Unknown placeholder"}
