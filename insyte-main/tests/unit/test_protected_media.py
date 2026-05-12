"""Tests for the production media protection view."""

from pathlib import Path

import pytest
from django.http import Http404
from django.test import RequestFactory, override_settings

from responsehandling.urls import protected_media_serve
from tests.factories import UserFactory


@pytest.mark.django_db()
@override_settings(MEDIA_ROOT="/tmp")
def test_protected_media_redirects_anonymous_user(rf: RequestFactory) -> None:
    """Anonymous users should be sent to login instead of reading media files."""
    from django.contrib.auth.models import AnonymousUser

    request = rf.get("/media/uploads/report.csv")
    request.user = AnonymousUser()

    response = protected_media_serve(request, "uploads/report.csv")

    assert response.status_code == 302
    assert response["Location"] == "/auth/login/?next=%2Fmedia%2Fuploads%2Freport.csv"


@pytest.mark.django_db()
def test_protected_media_blocks_authenticated_user_without_access(
    rf: RequestFactory, tmp_path: Path
) -> None:
    """Authenticated users without system access should not read arbitrary files."""
    media_path = tmp_path / "uploads" / "report.csv"
    media_path.parent.mkdir(parents=True)
    media_path.write_text("secret")

    request = rf.get("/media/uploads/report.csv")
    request.user = UserFactory(is_staff=False)

    with (
        override_settings(MEDIA_ROOT=tmp_path),
        pytest.raises(Http404),
    ):
        protected_media_serve(request, "uploads/report.csv")


@pytest.mark.django_db()
def test_protected_media_allows_staff_user(rf: RequestFactory, tmp_path: Path) -> None:
    """Staff users can access files under MEDIA_ROOT."""
    media_path = tmp_path / "uploads" / "report.csv"
    media_path.parent.mkdir(parents=True)
    media_path.write_text("secret")

    request = rf.get("/media/uploads/report.csv")
    request.user = UserFactory(is_staff=True)

    with override_settings(MEDIA_ROOT=tmp_path):
        response = protected_media_serve(request, "uploads/report.csv")

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"secret"
