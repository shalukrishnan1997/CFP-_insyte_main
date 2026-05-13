"""Session-scoped NDJSON debug logging (Cursor / IDE debugging sessions only)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from typing import Any

from django.conf import settings

_DEFAULT_LOG = os.path.join(
    tempfile.gettempdir(), "responsehandling-agent-debug.ndjson"
)

_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "token",
    "secret",
    "auth",
    "cookie",
)
_PLAINTEXT_IDENT_FIELDS = frozenset(
    {
        "username",
        "email",
        "email_address",
        "subject",
        "body",
        "summary",
        "authorization",
        "csrf",
        "otp",
        "credential",
        "firstname",
        "lastname",
        "first_name",
        "last_name",
    }
)


def agent_debug_logging_enabled(*, bypass_settings: bool | None = None) -> bool:
    """Whether agent debug instrumentation may write diagnostics.

    Writes are allowed when Django ``DEBUG`` is true or when
    ``settings.INSYTE_AGENT_DEBUG`` is explicitly enabled (see ``.env.example``).
    Tests may pass ``bypass_settings=True`` to force logging when ``DEBUG`` is off.
    """
    if bypass_settings:
        return True
    if getattr(settings, "DEBUG", False):
        return True
    return bool(getattr(settings, "INSYTE_AGENT_DEBUG", False))


def _redact_debug_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Strip obvious secrets / identifiers outside of Django ``DEBUG`` mode."""
    out: dict[str, Any] = {}
    for key, raw_value in data.items():
        lk = key.lower().replace("-", "_")
        if lk in _PLAINTEXT_IDENT_FIELDS or any(
            frag in lk for frag in _SENSITIVE_KEY_FRAGMENTS
        ):
            out[key] = "[redacted]"
            continue
        out[key] = copy.deepcopy(raw_value)
    return out


def agent_debug_log(
    location: str,
    message: str,
    hypothesis_id: str,
    data: dict[str, Any],
    *,
    session_id: str = "55ea46",
    run_id: str = "dashboard-timeout",
    bypass_settings: bool | None = None,
) -> None:
    """Append one NDJSON line when agent debug logging is enabled.

    Automatically redacts payloads when ``DEBUG`` is false so staged or
    production environments that deliberately set ``INSYTE_AGENT_DEBUG``
    cannot accidentally leak identifiers or bearer material.
    """
    if not agent_debug_logging_enabled(bypass_settings=bypass_settings):
        return

    payload_data = data
    if not getattr(settings, "DEBUG", False):
        payload_data = _redact_debug_payload(dict(data))

    path = os.environ.get("DEBUG_AGENT_LOG_PATH", _DEFAULT_LOG)
    payload = {
        "sessionId": session_id,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": payload_data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(path, "a", encoding="utf-8") as fs:
            # default=str: user.pk may be UUID; debug payloads must never break the request
            fs.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass
