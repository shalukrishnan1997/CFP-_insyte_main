"""Tests for storage-backed letter download and listing helpers."""

from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import RequestFactory, override_settings

from letters.admin_views import download_batch_file
from letters.models import LetterBatch, LetterTemplate
from letters.view_helpers import collect_generated_letters
from tests.factories import CampaignFactory, UserFactory


def _create_letter_batch() -> tuple[LetterBatch, object]:
    """Create a minimal batch with a saved template file."""
    user = UserFactory(is_staff=True, is_superuser=True)
    campaign = CampaignFactory(created_by=user)
    template = LetterTemplate.objects.create(
        name="Thank You",
        campaign=campaign,
        created_by=user,
    )
    template.file.save("template.docx", ContentFile(b"template-bytes"), save=True)
    batch = LetterBatch.objects.create(
        campaign=campaign,
        template=template,
        batch_number=1,
        created_by=user,
    )
    return batch, user


@pytest.mark.django_db()
@override_settings(MEDIA_ROOT="/tmp")
def test_collect_generated_letters_reads_storage_relative_paths() -> None:
    """Generated letter listings should come from storage-backed batch paths."""
    batch, _user = _create_letter_batch()
    storage_name = f"generated_letters/{batch.campaign_id}/letters_batch1_THANKS_file1_20260402.docx"
    default_storage.save(storage_name, ContentFile(b"letter-bytes"))
    batch.output_files = [storage_name]
    batch.save(update_fields=["output_files"])

    letters = collect_generated_letters(batch.campaign_id)

    assert len(letters) == 1
    assert letters[0]["filename"] == Path(storage_name).name
    assert letters[0]["size"] == len(b"letter-bytes")


@pytest.mark.django_db()
def test_download_batch_file_supports_legacy_absolute_media_paths(
    rf: RequestFactory, tmp_path: Path
) -> None:
    """Older absolute output paths should still resolve through storage helpers."""
    batch, user = _create_letter_batch()
    relative_name = f"generated_letters/{batch.campaign_id}/letters_batch1_THANKS_file1_20260402.docx"

    with override_settings(MEDIA_ROOT=tmp_path):
        default_storage.save(relative_name, ContentFile(b"legacy-letter"))
        absolute_path = str(tmp_path / relative_name)
        batch.output_files = [absolute_path]
        batch.save(update_fields=["output_files"])

        request = rf.get(f"/admin/letter-setup/batch/{batch.id}/download/0/")
        request.user = user
        response = download_batch_file(request, str(batch.id), 0)

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"legacy-letter"
