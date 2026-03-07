"""Tests for GPU keepalive command generation."""

from __future__ import annotations

from inspire.cli.utils.keepalive import (
    KEEPALIVE_LOG,
    KEEPALIVE_STARTED_MARKER,
    PID_FILE,
    get_keepalive_command,
)


def test_get_keepalive_command_uses_python_fallback_and_marker() -> None:
    command = get_keepalive_command(completion_marker=KEEPALIVE_STARTED_MARKER)

    assert "\n" not in command
    assert "command -v python3 || command -v python || true" in command
    assert 'nohup "$PYTHON_BIN" -u -c ' in command
    assert "base64.b64decode" in command
    assert KEEPALIVE_LOG in command
    assert PID_FILE in command
    assert KEEPALIVE_STARTED_MARKER in command
