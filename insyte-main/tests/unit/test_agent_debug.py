"""Tests for deployment-safe agent debug defaults & production gating."""

from __future__ import annotations

import pytest
from django.test import override_settings

from core import agent_debug


def test_default_agent_debug_log_path_uses_temp_directory() -> None:
    """Default debug log path should not assume a developer workspace path."""
    assert agent_debug._DEFAULT_LOG.endswith("responsehandling-agent-debug.ndjson")


@pytest.mark.parametrize(
    ("debug_flag", "insyte_flag", "expect_write"),
    [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
)
def test_agent_debug_respects_flags(
    tmp_path,
    monkeypatch,
    debug_flag,
    insyte_flag,
    expect_write,
) -> None:
    """Writes only when Django DEBUG or INSYTE_AGENT_DEBUG permits."""
    log_path = tmp_path / "dbg.ndjson"
    monkeypatch.setenv("DEBUG_AGENT_LOG_PATH", str(log_path))

    with override_settings(DEBUG=debug_flag, INSYTE_AGENT_DEBUG=insyte_flag):
        agent_debug.agent_debug_log(
            "t.loc",
            "ping",
            "H",
            {"safe_metric": 1},
            bypass_settings=False,
        )

    assert log_path.exists() is expect_write


@override_settings(DEBUG=False, INSYTE_AGENT_DEBUG=True)
def test_agent_debug_redacts_sensitive_keys_when_debug_off(
    tmp_path,
    monkeypatch,
) -> None:
    """Manual ``INSYTE_AGENT_DEBUG`` staging must not persist raw usernames."""
    log_path = tmp_path / "redact.ndjson"
    monkeypatch.setenv("DEBUG_AGENT_LOG_PATH", str(log_path))

    agent_debug.agent_debug_log(
        "t",
        "m",
        "H",
        {"username": "super-secret", "path": "/dashboard/"},
    )

    dumped = log_path.read_text(encoding="utf-8")
    assert "super-secret" not in dumped
    assert "[redacted]" in dumped


@override_settings(DEBUG=False, INSYTE_AGENT_DEBUG=False)
def test_bypass_settings_forces_logging(tmp_path, monkeypatch) -> None:
    """Instrumentation tests may temporarily force logging explicitly."""
    log_path = tmp_path / "forced.ndjson"
    monkeypatch.setenv("DEBUG_AGENT_LOG_PATH", str(log_path))

    agent_debug.agent_debug_log(
        "t",
        "forced",
        "H",
        {"marker": "x"},
        bypass_settings=True,
    )

    assert log_path.exists()
    body = log_path.read_text(encoding="utf-8")
    assert "marker" in body
