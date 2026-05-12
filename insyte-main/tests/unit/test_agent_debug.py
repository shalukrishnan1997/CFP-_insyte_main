"""Tests for deployment-safe agent debug defaults."""

import os
import tempfile

from core import agent_debug


def test_default_agent_debug_log_path_uses_temp_directory() -> None:
    """Default debug log path should not assume a developer workspace path."""
    assert (
        os.path.join(
            tempfile.gettempdir(),
            "responsehandling-agent-debug.ndjson",
        )
        == agent_debug._DEFAULT_LOG
    )
