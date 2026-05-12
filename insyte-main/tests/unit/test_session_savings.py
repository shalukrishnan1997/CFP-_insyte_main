"""Smoke tests for SESSION_SAVE_EVERY_REQUEST tuning.

These guard the production scaling change shipped alongside the Gunicorn
worker bump (see :file:`Dockerfile`): a request whose view does not touch
``request.session`` must NOT round-trip the session store. Re-enabling
per-request saves would put avoidable Redis ``SETEX`` traffic on the hot
path under 100 concurrent QA reviewers.
"""

from __future__ import annotations

from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.backends.cache import SessionStore as CacheSessionStore
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory


def test_setting_is_disabled() -> None:
    """``SESSION_SAVE_EVERY_REQUEST`` must stay False in every environment."""
    assert settings.SESSION_SAVE_EVERY_REQUEST is False


def test_session_cookie_age_floor() -> None:
    """``SESSION_COOKIE_AGE`` must be at least 4 hours.

    Because ``SESSION_SAVE_EVERY_REQUEST`` is False, sessions do not slide on
    read-only navigation. Compensate with a longer cookie age so an active QA
    reviewer who only clicks read-only links is not logged out mid-session.
    """
    assert settings.SESSION_COOKIE_AGE >= 14400


def test_unmodified_session_does_not_trigger_save(rf: RequestFactory) -> None:
    """A view that only reads the session does not persist on response.

    Django's ``SessionMiddleware.process_response`` only calls
    ``request.session.save()`` when the session was modified OR
    ``SESSION_SAVE_EVERY_REQUEST`` is True. With the setting flipped to
    False, an idempotent navigation should leave the session store alone.
    """

    def view(request: HttpRequest) -> HttpResponse:
        _ = request.session.get("any-key")
        return HttpResponse(b"ok")

    middleware = SessionMiddleware(view)
    request = rf.get("/")

    with patch.object(CacheSessionStore, "save", autospec=True) as mock_save:
        response = middleware(request)

    assert response.status_code == 200
    mock_save.assert_not_called()


def test_modified_session_still_triggers_save(rf: RequestFactory) -> None:
    """Sanity check the inverse: writes still persist after the flip.

    This pins the behaviour we explicitly want to keep — login, logout,
    flash messages, etc. all set ``session.modified`` and must continue
    to round-trip to Redis.
    """

    def view(request: HttpRequest) -> HttpResponse:
        request.session["touched"] = True
        return HttpResponse(b"ok")

    middleware = SessionMiddleware(view)
    request = rf.get("/")

    with patch.object(CacheSessionStore, "save", autospec=True) as mock_save:
        response = middleware(request)

    assert response.status_code == 200
    mock_save.assert_called()
