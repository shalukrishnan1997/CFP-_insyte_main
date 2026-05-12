"""Unit tests for scanned form URL resolution on the Donation model.

All scans live in Cloudflare R2. The authoritative URL is always
``ScanPlaceholder.image_url``, set by ``ScanProcessingService`` at upload time.
There is no local-disk fallback.
"""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import django
import pytest
from django.middleware.clickjacking import XFrameOptionsMiddleware
from django.test import Client, RequestFactory
from django.urls import reverse

django.setup()

from scans.donation_scan import DonationScanService  # noqa: E402
from tests.factories import (  # noqa: E402
    DonationFactory,
    DonorFactory,
    ScanPlaceholderFactory,
    UserFactory,
)

R2_URL = "https://cdn.example.com/ScanOutput/spring25/cheque/URN001.jpg"


# ---------------------------------------------------------------------------
# get_scanned_form_url — require_existing=True
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_url_returns_placeholder_r2_url() -> None:
    """Returns the first internal placeholder page-image route when a scan exists."""
    donation = DonationFactory()
    placeholder = ScanPlaceholderFactory(donation=donation, image_url=R2_URL)

    url = DonationScanService.get_scanned_form_url(donation, require_existing=True)
    assert url is not None
    assert url.startswith(
        reverse("custom_admin:scan_image_serve")
        + f"?key=ScanOutput/appeal/cheque/{placeholder.urn}.jpg&id={placeholder.id}&token="
    )


@pytest.mark.django_db
def test_get_url_returns_none_when_no_placeholder() -> None:
    """Returns None when the donation has no scan_placeholder at all."""
    donation = DonationFactory()

    assert (
        DonationScanService.get_scanned_form_url(donation, require_existing=True)
        is None
    )


@pytest.mark.django_db
def test_get_url_returns_none_when_placeholder_url_blank() -> None:
    """Returns None when scan_placeholder exists but image_url is empty."""
    donation = DonationFactory()
    ScanPlaceholderFactory(donation=donation, image_url="", image_path="")

    assert (
        DonationScanService.get_scanned_form_url(donation, require_existing=True)
        is None
    )


@pytest.mark.django_db
def test_get_url_parses_virtual_pdf_page_key() -> None:
    """Virtual PDF page keys resolve through the signed page-image proxy route."""
    virtual_key = "campaign/package/abc 1.pdf::pdf_page::0001"

    donation = DonationFactory()
    placeholder = ScanPlaceholderFactory(
        donation=donation,
        image_url="",
        image_path=virtual_key,
    )

    url = DonationScanService.get_scanned_form_url(donation, require_existing=True)

    assert url is not None
    assert url.startswith(
        reverse("custom_admin:scan_image_serve")
        + f"?key=campaign/package/abc%201.pdf::pdf_page::0001&id={placeholder.id}&token="
    )


@pytest.mark.django_db
def test_get_page_urls_returns_all_placeholder_pages_in_order() -> None:
    """Multi-page placeholders expose each page through the signed image proxy."""
    donation = DonationFactory()
    placeholder = ScanPlaceholderFactory(
        donation=donation,
        image_url="",
        image_path="ScanOutput/demo/form_doc_0001.png",
        page_keys=[
            "ScanOutput/demo/form_doc_0001.png",
            "ScanOutput/demo/form_doc_0002.png",
            "ScanOutput/demo/form_doc_0003.png",
        ],
    )

    page_urls = DonationScanService.get_scanned_form_page_urls(
        donation, require_existing=True
    )

    assert len(page_urls) == 3
    assert page_urls[0].startswith(
        reverse("custom_admin:scan_image_serve")
        + f"?key=ScanOutput/demo/form_doc_0001.png&id={placeholder.id}&token="
    )
    assert page_urls[1].startswith(
        reverse("custom_admin:scan_image_serve")
        + f"?key=ScanOutput/demo/form_doc_0002.png&id={placeholder.id}&token="
    )


@pytest.mark.django_db
def test_scanned_form_lookup_returns_placeholder_page_urls() -> None:
    """Lookup returns canonical page URLs when a matching placeholder-backed donation exists."""
    from custom_admin import api_views

    staff_user = UserFactory(is_staff=True)

    donor = DonorFactory(urn="URNLOOKUP001")
    donation = DonationFactory(donor=donor)
    placeholder = ScanPlaceholderFactory(
        donation=donation,
        urn=donor.urn,
        image_url="",
        image_path="ScanOutput/demo/lookup_0001.png",
        page_keys=[
            "ScanOutput/demo/lookup_0001.png",
            "ScanOutput/demo/lookup_0002.png",
        ],
    )

    request = RequestFactory().get(
        reverse("custom_admin:scanned_form_lookup"),
        {"campaign_id": str(donation.campaign_id), "urn": donor.urn},
    )
    request.user = staff_user

    response = api_views.scanned_form_lookup(request)

    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["has_image"] is True
    assert len(payload["page_urls"]) == 2
    assert payload["image_url"] == payload["page_urls"][0]
    assert payload["page_urls"][0].startswith(
        reverse("custom_admin:scan_image_serve")
        + f"?key=ScanOutput/demo/lookup_0001.png&id={placeholder.id}&token="
    )


@pytest.mark.django_db
def test_scanned_form_lookup_batch_id_scopes_across_duplicate_urns() -> None:
    """Locks in the cross-batch contamination fix: when the same donor URN
    appears in more than one batch in the same campaign (e.g. card + cheque
    batches for the same donor), passing ``batch_id`` constrains the lookup
    to that batch's placeholder so the wrong-batch image cannot be served.
    """
    from custom_admin import api_views
    from tests.factories import (
        CampaignFactory,
        DonationBatchFactory,
        ScanBatchFactory,
    )

    staff_user = UserFactory(is_staff=True)
    campaign = CampaignFactory()
    donor = DonorFactory(urn="URNDUP001", client=campaign.client)

    cheque_scan_batch = ScanBatchFactory(campaign=campaign, payment_method="cheque")
    cheque_donation_batch = DonationBatchFactory(
        campaign=campaign, default_payment_method="cheque"
    )
    cheque_donation = DonationFactory(
        donor=donor, campaign=campaign, batch=cheque_donation_batch
    )
    cheque_placeholder = ScanPlaceholderFactory(
        donation=cheque_donation,
        batch=cheque_scan_batch,
        urn=donor.urn,
        image_url="",
        image_path="ScanOutput/demo/cheque_page_0001.png",
        page_keys=["ScanOutput/demo/cheque_page_0001.png"],
    )

    card_scan_batch = ScanBatchFactory(
        campaign=campaign, payment_method="card", scan_form_type="simplex"
    )
    card_donation_batch = DonationBatchFactory(
        campaign=campaign, default_payment_method="card"
    )
    card_donation = DonationFactory(
        donor=donor, campaign=campaign, batch=card_donation_batch
    )
    card_placeholder = ScanPlaceholderFactory(
        donation=card_donation,
        batch=card_scan_batch,
        urn=donor.urn,
        image_url="",
        image_path="ScanOutput/demo/card_page_0001.png",
        page_keys=["ScanOutput/demo/card_page_0001.png"],
    )

    # Card batch was created last → without ``batch_id`` the unscoped query
    # picks the most-recently-created donation. Confirm we can pin it to the
    # cheque batch by passing the cheque scan batch's id.
    request = RequestFactory().get(
        reverse("custom_admin:scanned_form_lookup"),
        {
            "campaign_id": str(campaign.id),
            "urn": donor.urn,
            "batch_id": str(cheque_scan_batch.id),
        },
    )
    request.user = staff_user
    response = api_views.scanned_form_lookup(request)
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["has_image"] is True
    assert payload["page_urls"][0].startswith(
        reverse("custom_admin:scan_image_serve")
        + f"?key=ScanOutput/demo/cheque_page_0001.png&id={cheque_placeholder.id}&token="
    )

    # And pin the same lookup to the card scan batch — different URL.
    request = RequestFactory().get(
        reverse("custom_admin:scanned_form_lookup"),
        {
            "campaign_id": str(campaign.id),
            "urn": donor.urn,
            "batch_id": str(card_scan_batch.id),
        },
    )
    request.user = staff_user
    response = api_views.scanned_form_lookup(request)
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["has_image"] is True
    assert payload["page_urls"][0].startswith(
        reverse("custom_admin:scan_image_serve")
        + f"?key=ScanOutput/demo/card_page_0001.png&id={card_placeholder.id}&token="
    )


# ---------------------------------------------------------------------------
# get_scanned_form_url — require_existing=False (predicted path for viewer hint)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_url_require_false_returns_predicted_path() -> None:
    """require_existing=False returns the predicted /media/ path for the viewer hint.

    This is shown in the viewer when no scan is found so the operator knows
    what path was expected.
    """
    donation = DonationFactory()

    url = DonationScanService.get_scanned_form_url(donation, require_existing=False)

    assert url is not None
    assert url.startswith("/media/")


# ---------------------------------------------------------------------------
# has_scanned_form
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_has_scanned_form_true_when_placeholder_has_url() -> None:
    """True when scan_placeholder.image_url is set."""
    donation = DonationFactory()
    ScanPlaceholderFactory(donation=donation, image_url=R2_URL)

    assert DonationScanService.has_scanned_form(donation) is True


@pytest.mark.django_db
def test_has_scanned_form_false_when_no_placeholder() -> None:
    """False when no scan_placeholder exists."""
    donation = DonationFactory()

    assert DonationScanService.has_scanned_form(donation) is False


@pytest.mark.django_db
def test_has_scanned_form_false_when_placeholder_url_blank() -> None:
    """False when scan_placeholder exists but image_url is blank."""
    donation = DonationFactory()
    ScanPlaceholderFactory(donation=donation, image_url="", image_path="")

    assert DonationScanService.has_scanned_form(donation) is False


# ---------------------------------------------------------------------------
# ScanPlaceholder factory smoke test
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_scan_placeholder_factory_creates_correctly() -> None:
    """ScanPlaceholderFactory creates a linked record with a valid R2 URL."""
    donation = DonationFactory()
    placeholder = ScanPlaceholderFactory(donation=donation)

    assert placeholder.pk is not None
    assert placeholder.image_url.startswith("https://")
    assert placeholder.donation == donation
    assert donation.scan_placeholder == placeholder


@pytest.mark.django_db
def test_placeholder_pdf_view_omits_x_frame_options_for_qa_iframe(
    client: Client,
) -> None:
    """The scan PDF proxy must be embeddable in the QA iframe."""
    from pypdf import PdfWriter

    from scans import api_views as scan_api_views

    staff_user = UserFactory(is_staff=True)

    placeholder = ScanPlaceholderFactory(
        batch__created_by=staff_user,
        image_url="",
        image_path="ScanOutput/demo/form.pdf::pdf_page::0001",
        page_keys=["ScanOutput/demo/form.pdf::pdf_page::0001"],
    )

    pdf_buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(pdf_buffer)

    mock_r2_client = MagicMock()
    mock_r2_client.get_object.return_value = {"Body": BytesIO(pdf_buffer.getvalue())}

    request = RequestFactory().get(
        reverse("custom_admin:scan_placeholder_pdf"),
        {
            "id": str(placeholder.id),
            "token": scan_api_views.build_scan_view_token(str(placeholder.id)),
        },
    )
    request.user = type("Anon", (), {"is_authenticated": False})()

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.get_r2_client", return_value=mock_r2_client),
    ):
        response = scan_api_views.scan_placeholder_pdf(request)

    response = XFrameOptionsMiddleware(lambda req: response).process_response(
        request, response
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert "X-Frame-Options" not in response


@pytest.mark.django_db
def test_placeholder_pdf_view_supports_image_page_keys() -> None:
    """Image-backed split pages should still render through the PDF proxy."""
    from PIL import Image
    from pypdf import PdfReader

    from scans import api_views as scan_api_views

    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/split/form_doc_0001.png",
        page_keys=["ScanOutput/demo/split/form_doc_0001.png"],
    )

    image_buffer = BytesIO()
    Image.new("RGB", (180, 180), color="white").save(image_buffer, format="PNG")

    mock_r2_client = MagicMock()
    mock_r2_client.get_object.return_value = {"Body": BytesIO(image_buffer.getvalue())}

    request = RequestFactory().get(
        reverse("custom_admin:scan_placeholder_pdf"),
        {
            "id": str(placeholder.id),
            "token": scan_api_views.build_scan_view_token(str(placeholder.id)),
        },
    )
    request.user = type("Anon", (), {"is_authenticated": False})()

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.get_r2_client", return_value=mock_r2_client),
    ):
        response = scan_api_views.scan_placeholder_pdf(request)

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    reader = PdfReader(BytesIO(response.content))
    assert len(reader.pages) == 1


@pytest.mark.django_db
def test_placeholder_pdf_view_rejects_missing_auth_and_token() -> None:
    """The scan PDF proxy must not be public without a signed token."""
    from scans import api_views as scan_api_views

    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/form.pdf::pdf_page::0001",
        page_keys=["ScanOutput/demo/form.pdf::pdf_page::0001"],
    )

    request = RequestFactory().get(
        reverse("custom_admin:scan_placeholder_pdf"),
        {"id": str(placeholder.id)},
    )
    request.user = type("Anon", (), {"is_authenticated": False})()

    response = scan_api_views.scan_placeholder_pdf(request)

    assert response.status_code == 403


@pytest.mark.django_db
def test_scan_placeholder_view_shows_pending_redaction_scan_to_staff(
    client: Client, redaction_required_all: object
) -> None:
    """Staff view-scan page should expose QA-accessible pending-redaction URLs."""
    del redaction_required_all

    staff_user = UserFactory(is_staff=True)
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/pending_view_0001.png",
        page_keys=["ScanOutput/demo/pending_view_0001.png"],
        redaction_status="pending",
    )

    client.force_login(staff_user)
    response = client.get(
        reverse("custom_admin:scan_placeholder_view"),
        {"id": str(placeholder.id)},
    )

    assert response.status_code == 200
    assert response.context is not None
    assert response.context["has_image"] is True
    assert "allow_pending_redaction=1" in response.context["page_urls_json"]
    assert reverse("custom_admin:scan_image_serve") in response.context["image_url"]


@pytest.mark.django_db
def test_pending_redaction_urls_stay_hidden_outside_qa(
    redaction_required_all: object,
) -> None:
    """Pending redaction scans should not leak outside the QA redaction flow."""
    del redaction_required_all

    donation = DonationFactory()
    ScanPlaceholderFactory(
        donation=donation,
        image_url="",
        image_path="ScanOutput/demo/pending_0001.png",
        page_keys=["ScanOutput/demo/pending_0001.png"],
        redaction_status="pending",
    )

    assert (
        DonationScanService.get_scanned_form_page_urls(donation, require_existing=True)
        == []
    )
    assert (
        DonationScanService.get_scanned_form_url(donation, require_existing=True)
        is None
    )


@pytest.mark.django_db
def test_pending_redaction_urls_are_available_to_qa_reviewers(
    redaction_required_all: object,
) -> None:
    """QA review can request same-origin page URLs before redaction is completed."""
    del redaction_required_all

    staff_user = UserFactory(is_staff=True)
    donation = DonationFactory()
    placeholder = ScanPlaceholderFactory(
        donation=donation,
        image_url="",
        image_path="ScanOutput/demo/pending_qa_0001.png",
        page_keys=["ScanOutput/demo/pending_qa_0001.png"],
        redaction_status="pending",
    )

    page_urls = DonationScanService.get_scanned_form_page_urls(
        donation,
        require_existing=True,
        user=staff_user,
        allow_pending_redaction=True,
    )

    assert len(page_urls) == 1
    assert page_urls[0].startswith(
        reverse("custom_admin:scan_image_serve")
        + f"?key=ScanOutput/demo/pending_qa_0001.png&id={placeholder.id}&token="
    )
    assert "allow_pending_redaction=1" in page_urls[0]
    assert "inline=1" in page_urls[0]


@pytest.mark.django_db
def test_scan_image_serve_inline_proxy_returns_same_origin_bytes_for_qa_redaction(
    redaction_required_all: object,
) -> None:
    """QA redaction requests should receive inline bytes instead of an R2 redirect."""
    from scans import api_views as scan_api_views

    del redaction_required_all
    staff_user = UserFactory(is_staff=True)
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="ScanOutput/demo/inline_redaction.png",
        page_keys=["ScanOutput/demo/inline_redaction.png"],
        redaction_status="pending",
    )

    request = RequestFactory().get(
        reverse("custom_admin:scan_image_serve"),
        {
            "key": "ScanOutput/demo/inline_redaction.png",
            "id": str(placeholder.id),
            "token": scan_api_views.build_scan_view_token(str(placeholder.id)),
            "allow_pending_redaction": "1",
            "inline": "1",
        },
    )
    request.user = staff_user

    mock_r2_client = MagicMock()
    mock_r2_client.get_object.return_value = {
        "Body": BytesIO(b"image-bytes"),
        "ContentType": "image/png",
    }

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.get_r2_client", return_value=mock_r2_client),
    ):
        response = scan_api_views.scan_image_serve(request)

    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content == b"image-bytes"
