"""Session-scoped NDJSON debug logging (Cursor debug mode)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

_DEFAULT_LOG = os.path.join(
    tempfile.gettempdir(), "responsehandling-agent-debug.ndjson"
)


def agent_debug_log(
    location: str,
    message: str,
    hypothesis_id: str,
    data: dict[str, Any],
    *,
    session_id: str = "55ea46",
    run_id: str = "dashboard-timeout",
) -> None:
    """Append one NDJSON line; path from DEBUG_AGENT_LOG_PATH or default (dev workspace)."""
    path = os.environ.get("DEBUG_AGENT_LOG_PATH", _DEFAULT_LOG)
    payload = {
        "sessionId": session_id,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            # default=str: user.pk may be UUID; debug payloads must never break the request
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass
