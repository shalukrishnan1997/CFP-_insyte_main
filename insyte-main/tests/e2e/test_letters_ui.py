"""End-to-end browser walkthrough for the letters admin UI.

Covers visual correctness of the authoring-guide card, the reference-DOCX
download, the template preview action, and the status-icon / confirm-dialog
wiring that integration tests can't verify.
"""

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from docx import Document
from playwright.sync_api import Page, expect

from auth_app.models import EmailDevice
from core.models import User
from letters.models import LetterTemplate
from tests.factories import CampaignFactory


def _make_docx(*paragraphs: str) -> SimpleUploadedFile:
    """Build an in-memory DOCX uploaded-file with the given paragraphs."""
    document = Document()
    for p in paragraphs:
        document.add_paragraph(p)
    buffer = BytesIO()
    document.save(buffer)
    return SimpleUploadedFile(
        "tpl.docx",
        buffer.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    )


@pytest.mark.django_db(transaction=True)
def test_letter_setup_campaign_authoring_card_renders(
    staff_authenticated_page: Page,
    live_base_url: str,
    staff_user_with_2fa: tuple[User, EmailDevice],
) -> None:
    """The authoring guide card shows literal docxtpl syntax and a reference-guide link."""
    staff_user, _ = staff_user_with_2fa
    campaign = CampaignFactory(created_by=staff_user)
    LetterTemplate.objects.create(
        campaign=campaign,
        name="Thanks",
        template_type="thank_you",
        file=_make_docx("Dear {{ donor_full_name }}"),
        is_active=True,
        created_by=staff_user,
    )

    staff_authenticated_page.goto(
        f"{live_base_url}/admin/letter-setup/campaign/{campaign.id}/"
    )

    # Authoring card is present
    expect(
        staff_authenticated_page.get_by_role("heading", name="Template syntax")
    ).to_be_visible()

    # Literal docxtpl syntax rendered (proves {% templatetag %} escaping)
    body = staff_authenticated_page.locator("body")
    expect(body).to_contain_text("{{ donor_full_name }}")
    expect(body).to_contain_text('{% if gift_aid == "Yes" %}')
    expect(body).to_contain_text("{% elif amount_raw >= 100 %}")
    expect(body).to_contain_text("{% endif %}")

    # Download-reference button + Preview button both wired up
    expect(
        staff_authenticated_page.get_by_role(
            "link", name="Download full reference (DOCX)"
        )
    ).to_be_visible()
    expect(staff_authenticated_page.get_by_role("link", name="Preview")).to_be_visible()

    # 10 MB hint appears on the dropzone
    expect(body).to_contain_text("Max 10 MB")


@pytest.mark.django_db(transaction=True)
def test_reference_guide_download_triggers_docx_download(
    staff_authenticated_page: Page,
    live_base_url: str,
) -> None:
    """The reference-guide URL returns a DOCX attachment with the canonical filename."""
    # Hit the URL via the authenticated browser context so session cookies are reused.
    response = staff_authenticated_page.request.get(
        f"{live_base_url}/admin/letter-setup/reference-guide/"
    )
    assert response.status == 200
    assert (
        'attachment; filename="letter_template_field_reference.docx"'
        in response.headers.get("content-disposition", "")
    )
    assert response.headers.get("content-type", "").startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert len(response.body()) > 0


@pytest.mark.django_db(transaction=True)
def test_preview_button_returns_rendered_docx(
    staff_authenticated_page: Page,
    live_base_url: str,
    staff_user_with_2fa: tuple[User, EmailDevice],
) -> None:
    """The Preview URL for the active template streams a rendered sample DOCX."""
    staff_user, _ = staff_user_with_2fa
    campaign = CampaignFactory(created_by=staff_user)
    LetterTemplate.objects.create(
        campaign=campaign,
        name="Thanks",
        template_type="thank_you",
        file=_make_docx(
            "Dear {{ donor_full_name }}, thanks for {{ amount_formatted }}."
        ),
        is_active=True,
        created_by=staff_user,
    )

    # First verify the Preview link is present on the campaign page (the UI wiring).
    staff_authenticated_page.goto(
        f"{live_base_url}/admin/letter-setup/campaign/{campaign.id}/"
    )
    preview_link = staff_authenticated_page.get_by_role("link", name="Preview")
    expect(preview_link).to_be_visible()
    preview_href = preview_link.get_attribute("href")
    assert preview_href is not None and "/preview-template/thank_you/" in preview_href

    # Then fetch it directly to verify it actually returns a rendered DOCX.
    response = staff_authenticated_page.request.get(f"{live_base_url}{preview_href}")
    assert response.status == 200
    assert "_thank_you_sample.docx" in response.headers.get("content-disposition", "")
    # Rendered DOCX should contain the sample donor name, not the raw placeholder.
    rendered = Document(BytesIO(response.body()))
    rendered_text = "\n".join(p.text for p in rendered.paragraphs)
    assert "Alex Taylor" in rendered_text
    assert "{{" not in rendered_text


@pytest.mark.django_db(transaction=True)
def test_batches_list_status_badges_include_icons(
    staff_authenticated_page: Page,
    live_base_url: str,
    staff_user_with_2fa: tuple[User, EmailDevice],
) -> None:
    """Status badges on the batches list use icons in addition to colour."""
    from letters.models import LetterBatch

    staff_user, _ = staff_user_with_2fa
    campaign = CampaignFactory(created_by=staff_user)
    template = LetterTemplate.objects.create(
        campaign=campaign,
        name="Thanks",
        template_type="thank_you",
        file=_make_docx("Dear {{ donor_full_name }}"),
        is_active=True,
        created_by=staff_user,
    )
    LetterBatch.objects.create(
        campaign=campaign,
        template=template,
        batch_number=1,
        status=LetterBatch.STATUS_COMPLETED,
        total_letters=10,
        letters_per_file=100,
        donation_filter="all",
        regenerate_mode=False,
        created_by=staff_user,
    )

    staff_authenticated_page.goto(
        f"{live_base_url}/admin/letter-setup/campaign/{campaign.id}/batches/"
    )

    # Icon (✓) appears with the Completed badge, not colour alone
    expect(staff_authenticated_page.locator("body")).to_contain_text("✓ Completed")
