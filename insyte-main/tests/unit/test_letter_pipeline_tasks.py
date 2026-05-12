"""Unit tests for letter pipeline task entry points."""

from typing import Any

import pytest

from letters.models import LetterBatch, LetterTemplate
from letters.tasks import cancel_letter_batch
from tests.factories import CampaignFactory, UserFactory


def _create_template(
    campaign: Any,
    created_by: Any,
    *,
    template_type: str = "thank_you",
) -> LetterTemplate:
    """Create a minimal letter template for batch tests."""
    return LetterTemplate.objects.create(
        campaign=campaign,
        name="Pipeline Template",
        template_type=template_type,
        file="uploads/letter_templates/pipeline.docx",
        created_by=created_by,
    )


@pytest.mark.django_db()
class TestLetterPipelineTasks:
    """Regression tests for letter-batch cancellation."""

    def test_cancel_letter_batch_revokes_task_and_marks_cancelled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancellation task revokes active Celery task and marks batch cancelled."""
        user = UserFactory(is_staff=True, is_superuser=True)
        campaign = CampaignFactory(created_by=user)
        template = _create_template(campaign, user)
        batch = LetterBatch.objects.create(
            campaign=campaign,
            template=template,
            batch_number=9,
            status="processing",
            created_by=user,
            celery_task_id="celery-letter-task-001",
        )

        captured: dict[str, object] = {}

        class FakeAsyncResult:
            """Stub AsyncResult to capture revoke invocation."""

            def __init__(self, task_id: str) -> None:
                captured["task_id"] = task_id

            def revoke(self, terminate: bool) -> None:
                captured["terminate"] = terminate

        monkeypatch.setattr("letters.tasks.AsyncResult", FakeAsyncResult)

        result = cancel_letter_batch(str(batch.id))

        batch.refresh_from_db()
        assert result["success"] is True
        assert batch.status == "cancelled"
        assert captured == {
            "task_id": "celery-letter-task-001",
            "terminate": True,
        }
