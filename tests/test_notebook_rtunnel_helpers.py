"""Tests for REST API terminal creation, batch script, and _StepTimer helpers."""

from __future__ import annotations

import base64

import pytest

from inspire.platform.web.browser_api.rtunnel import (
    _StepTimer,
    _build_batch_setup_script,
    _create_terminal_via_api,
    _jupyter_server_base,
)


# ---------------------------------------------------------------------------
# _jupyter_server_base
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lab_url", "expected"),
    [
        # Standard: lab URL with /lab suffix
        (
            "https://notebook-inspire.example.com/lab",
            "https://notebook-inspire.example.com/",
        ),
        (
            "https://notebook-inspire.example.com/lab/",
            "https://notebook-inspire.example.com/",
        ),
        # Proxy-style: /notebook/lab/<id>/lab (JupyterLab route is the final /lab)
        (
            "https://example.com/api/v1/notebook/lab/nb-123/lab",
            "https://example.com/api/v1/notebook/lab/nb-123/",
        ),
        # Direct navigation URL (no /lab suffix) — no stripping
        (
            "https://example.com/api/v1/notebook/lab/nb-123/",
            "https://example.com/api/v1/notebook/lab/nb-123/",
        ),
        # Query parameters and fragments are stripped
        (
            "https://example.com/lab?token=abc#foo",
            "https://example.com/",
        ),
    ],
)
def test_jupyter_server_base(lab_url: str, expected: str) -> None:
    assert _jupyter_server_base(lab_url) == expected


# ---------------------------------------------------------------------------
# _create_terminal_via_api
# ---------------------------------------------------------------------------


class _DummyResponse:
    def __init__(self, status: int, data: dict | None = None) -> None:
        self.status = status
        self._data = data

    def json(self) -> dict:
        return self._data or {}


class _DummyRequest:
    def __init__(self, response: _DummyResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, int]] = []

    def post(self, url: str, headers: dict | None = None, timeout: int = 0) -> _DummyResponse:
        self.calls.append((url, timeout))
        return self._response


class _DummyContext:
    def __init__(self, request: _DummyRequest) -> None:
        self.request = request

    def cookies(self) -> list[dict]:
        return []


def test_create_terminal_via_api_success() -> None:
    resp = _DummyResponse(200, {"name": "3"})
    ctx = _DummyContext(_DummyRequest(resp))
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab")
    assert result == "3"
    assert len(ctx.request.calls) == 1
    assert ctx.request.calls[0][0] == "https://nb.example.com/api/terminals"


def test_create_terminal_via_api_201() -> None:
    resp = _DummyResponse(201, {"name": "1"})
    ctx = _DummyContext(_DummyRequest(resp))
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab/")
    assert result == "1"


def test_create_terminal_via_api_failure_status() -> None:
    resp = _DummyResponse(403, {})
    ctx = _DummyContext(_DummyRequest(resp))
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab")
    assert result is None


def test_create_terminal_via_api_exception() -> None:
    class _BrokenRequest:
        def post(self, url: str, headers: dict | None = None, timeout: int = 0) -> None:
            raise ConnectionError("network failure")

    ctx = _DummyContext(_BrokenRequest())  # type: ignore[arg-type]
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab")
    assert result is None


def test_create_terminal_via_api_proxy_url() -> None:
    """API URL should be derived from the server base, not the lab path."""
    resp = _DummyResponse(200, {"name": "2"})
    ctx = _DummyContext(_DummyRequest(resp))
    result = _create_terminal_via_api(ctx, "https://example.com/api/v1/notebook/lab/nb-123/lab")
    assert result == "2"
    assert ctx.request.calls[0][0] == "https://example.com/api/v1/notebook/lab/nb-123/api/terminals"


# ---------------------------------------------------------------------------
# _build_batch_setup_script
# ---------------------------------------------------------------------------


def test_build_batch_setup_script_roundtrip() -> None:
    commands = [
        "PORT=31337",
        "SSH_PORT=22222",
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh",
        'echo "INSPIRE_RTUNNEL_SETUP_DONE"',
    ]
    result = _build_batch_setup_script(commands)

    # Must be a single line
    assert "\n" not in result

    # Must start with echo and end with bash
    assert result.startswith("echo '")
    assert result.endswith("' | base64 -d | bash")

    # Extract and decode the base64 payload
    b64_payload = result[len("echo '") : result.index("' | base64 -d | bash")]
    decoded = base64.b64decode(b64_payload).decode()

    # Decoded script should contain all original commands
    for cmd in commands:
        assert cmd in decoded

    # Lines should be newline-separated
    lines = decoded.strip().split("\n")
    assert lines == commands


def test_build_batch_setup_script_empty() -> None:
    result = _build_batch_setup_script([])
    assert result.startswith("echo '")
    b64_payload = result[len("echo '") : result.index("' | base64 -d | bash")]
    decoded = base64.b64decode(b64_payload).decode()
    assert decoded == "\n"


# ---------------------------------------------------------------------------
# _StepTimer
# ---------------------------------------------------------------------------


def test_step_timer_disabled_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    timer = _StepTimer(enabled=False)
    timer.mark("a")
    timer.mark("b")
    timer.summary()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_step_timer_mark_returns_elapsed() -> None:
    timer = _StepTimer(enabled=False)
    result = timer.mark("x")
    assert result == 0.0
    assert isinstance(result, float)


def test_step_timer_records_steps(capsys: pytest.CaptureFixture[str]) -> None:
    timer = _StepTimer(enabled=True)
    timer.mark("alpha")
    timer.mark("beta")
    captured = capsys.readouterr()
    assert "[timing] alpha:" in captured.err
    assert "[timing] beta:" in captured.err


def test_step_timer_summary_format(capsys: pytest.CaptureFixture[str]) -> None:
    timer = _StepTimer(enabled=True)
    timer.mark("step_one")
    timer.mark("step_two")
    _ = capsys.readouterr()  # discard mark output

    timer.summary()
    captured = capsys.readouterr()
    assert "step_one" in captured.err
    assert "step_two" in captured.err
    assert "%" in captured.err
    assert "TOTAL" in captured.err


def test_step_timer_summary_empty_when_no_steps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timer = _StepTimer(enabled=True)
    timer.summary()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
