"""Tests for REST API terminal creation, batch script, and _StepTimer helpers."""

from __future__ import annotations

import base64

import pytest

from inspire.platform.web.browser_api import rtunnel as rtunnel_module
from inspire.platform.web.browser_api.rtunnel import (
    _StepTimer,
    _build_batch_setup_script,
    _build_terminal_websocket_url,
    _create_terminal_via_api,
    _extract_jupyter_token,
    _focus_terminal_input,
    _jupyter_server_base,
    _open_or_create_terminal,
    _send_terminal_command_via_websocket,
    _wait_for_terminal_surface,
    _wait_for_terminal_surface_progressive,
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


def test_create_terminal_via_api_playwright_exception() -> None:
    class _BrokenRequest:
        def post(self, url: str, headers: dict | None = None, timeout: int = 0) -> None:
            raise rtunnel_module.PlaywrightError("request failed")

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
# websocket url/token helpers
# ---------------------------------------------------------------------------


def test_extract_jupyter_token_prefers_query_token() -> None:
    lab_url = "https://example.com/jupyter/nb/path-token/lab?token=query-token"
    assert _extract_jupyter_token(lab_url) == "query-token"


def test_extract_jupyter_token_from_path() -> None:
    lab_url = "https://example.com/jupyter/nb-123/path-token/lab"
    assert _extract_jupyter_token(lab_url) == "path-token"


def test_extract_jupyter_token_missing() -> None:
    lab_url = "https://example.com/api/v1/notebook/lab/nb-123/"
    assert _extract_jupyter_token(lab_url) is None


def test_build_terminal_websocket_url_https() -> None:
    lab_url = "https://example.com/jupyter/nb-123/path-token/lab?token=query-token"
    ws_url = _build_terminal_websocket_url(lab_url, "7")
    assert (
        ws_url
        == "wss://example.com/jupyter/nb-123/path-token/terminals/websocket/7?token=query-token"
    )


def test_build_terminal_websocket_url_http_without_token() -> None:
    lab_url = "http://example.com/api/v1/notebook/lab/nb-123/"
    ws_url = _build_terminal_websocket_url(lab_url, "term-a")
    assert ws_url == "ws://example.com/api/v1/notebook/lab/nb-123/terminals/websocket/term-a"


def test_send_terminal_command_via_websocket_success() -> None:
    captured: dict = {}

    class _EvalPage:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            captured["script"] = script
            captured["payload"] = payload
            return True

    page = _EvalPage()
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
        timeout_ms=1234,
    )

    assert result is True
    assert "WebSocket" in captured["script"]
    assert captured["payload"]["wsUrl"] == "wss://example.test/terminals/websocket/1"
    assert captured["payload"]["stdinData"] == "echo hi\r"
    assert captured["payload"]["timeoutMs"] == 1234
    # promptTimeoutMs = min(1234 - 500, 3000) = 734
    assert captured["payload"]["promptTimeoutMs"] == 734


def test_send_terminal_command_via_websocket_exception() -> None:
    class _BrokenPage:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            raise RuntimeError("eval failed")

    page = _BrokenPage()
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
    )
    assert result is False


def test_send_terminal_command_via_websocket_playwright_exception() -> None:
    class _BrokenPage:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            raise rtunnel_module.PlaywrightError("eval failed")

    page = _BrokenPage()
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
    )
    assert result is False


# ---------------------------------------------------------------------------
# terminal surface and focus helpers
# ---------------------------------------------------------------------------


class _LocatorStub:
    def __init__(
        self,
        *,
        count: int = 0,
        wait_ok: bool = False,
    ) -> None:
        self._count = count
        self._wait_ok = wait_ok
        self.first = self
        self.wait_calls: list[tuple[str, int]] = []
        self.click_calls: list[int] = []

    def count(self) -> int:
        return self._count

    def wait_for(self, *, state: str, timeout: int) -> None:
        self.wait_calls.append((state, timeout))
        if not self._wait_ok:
            raise TimeoutError("not ready")

    def click(self, timeout: int = 0) -> None:
        self.click_calls.append(timeout)


class _FrameStub:
    def __init__(self, selectors: dict[str, _LocatorStub]) -> None:
        self._selectors = selectors

    def locator(self, selector: str) -> _LocatorStub:
        return self._selectors.setdefault(selector, _LocatorStub())


class _PageStub:
    def __init__(self) -> None:
        self.wait_calls: list[int] = []

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)


def test_wait_for_terminal_surface_uses_xterm_wait() -> None:
    xterm = _LocatorStub(wait_ok=True)
    frame = _FrameStub({".xterm": xterm})

    assert _wait_for_terminal_surface(frame, timeout_ms=1234) is True
    assert xterm.wait_calls == [("attached", 1234)]


def test_wait_for_terminal_surface_falls_back_to_textarea_count() -> None:
    frame = _FrameStub(
        {
            ".xterm": _LocatorStub(wait_ok=False),
            "textarea.xterm-helper-textarea": _LocatorStub(count=1),
        }
    )

    assert _wait_for_terminal_surface(frame, timeout_ms=500) is True


def test_wait_for_terminal_surface_progressive_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [0.0]
    attempts = {"count": 0}

    def fake_monotonic() -> float:
        return fake_time[0]

    def fake_wait_surface(_frame, *, timeout_ms: int) -> bool:  # type: ignore[no-untyped-def]
        assert timeout_ms > 0
        attempts["count"] += 1
        return attempts["count"] >= 3

    monkeypatch.setattr(rtunnel_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(rtunnel_module, "_wait_for_terminal_surface", fake_wait_surface)
    monkeypatch.setattr(rtunnel_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)

    class _ProgressPage:
        def __init__(self, now_ref: list[float]) -> None:
            self.now_ref = now_ref
            self.wait_calls: list[int] = []

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.wait_calls.append(timeout_ms)
            self.now_ref[0] += timeout_ms / 1000.0

    page = _ProgressPage(fake_time)
    frame = _FrameStub({})

    assert (
        _wait_for_terminal_surface_progressive(
            frame,
            page,
            total_timeout_ms=1200,
            poll_ms=200,
            tab_poke_interval_ms=500,
        )
        is True
    )
    assert attempts["count"] >= 3
    assert page.wait_calls


def test_wait_for_terminal_surface_progressive_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [0.0]

    def fake_monotonic() -> float:
        return fake_time[0]

    monkeypatch.setattr(rtunnel_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(
        rtunnel_module, "_wait_for_terminal_surface", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(rtunnel_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)

    class _ProgressPage:
        def __init__(self, now_ref: list[float]) -> None:
            self.now_ref = now_ref
            self.wait_calls: list[int] = []

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.wait_calls.append(timeout_ms)
            self.now_ref[0] += timeout_ms / 1000.0

    page = _ProgressPage(fake_time)
    frame = _FrameStub({})

    assert (
        _wait_for_terminal_surface_progressive(
            frame,
            page,
            total_timeout_ms=700,
            poll_ms=150,
            tab_poke_interval_ms=300,
        )
        is False
    )
    assert page.wait_calls
    assert fake_time[0] > 0.0


def test_focus_terminal_input_clicks_first_textarea() -> None:
    text_area = _LocatorStub(count=1, wait_ok=True)
    frame = _FrameStub({"textarea.xterm-helper-textarea": text_area})
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is True
    assert len(text_area.click_calls) == 1
    assert text_area.click_calls[0] > 0
    assert 40 in page.wait_calls


def test_focus_terminal_input_returns_false_when_unavailable() -> None:
    frame = _FrameStub({})
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is False


def test_open_or_create_terminal_returns_early_when_api_path_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"recover": 0, "entry": 0, "fallback": 0}

    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_rest_api",
        lambda **_kwargs: (True, True),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_recover_api_terminal_surface",
        lambda **_kwargs: calls.__setitem__("recover", calls["recover"] + 1) or False,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_wait_for_terminal_entry_point",
        lambda **_kwargs: calls.__setitem__("entry", calls["entry"] + 1),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: calls.__setitem__("fallback", calls["fallback"] + 1) or True,
    )

    assert _open_or_create_terminal(context=object(), page=object(), lab_frame=object()) is True
    assert calls["recover"] == 0
    assert calls["entry"] == 0
    assert calls["fallback"] == 0


def test_open_or_create_terminal_uses_dom_fallback_after_api_recovery_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_rest_api",
        lambda **_kwargs: (False, True),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_recover_api_terminal_surface",
        lambda **_kwargs: False,
    )

    def fake_wait_entry(*, lab_frame, api_term_created: bool) -> None:  # noqa: ANN001
        events.append(("entry", api_term_created))

    monkeypatch.setattr(rtunnel_module, "_wait_for_terminal_entry_point", fake_wait_entry)
    monkeypatch.setattr(
        rtunnel_module,
        "_dismiss_terminal_dialog_once",
        lambda **kwargs: events.append(("dismiss", kwargs["settle_ms"])) or False,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_click_terminal_tab",
        lambda *_args, **kwargs: events.append(("tab_click", kwargs["settle_ms"])) or True,
    )

    assert _open_or_create_terminal(context=object(), page=object(), lab_frame=object()) is True
    assert ("entry", True) in events
    assert ("tab_click", 80) in events


def test_open_or_create_terminal_handles_api_full_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(
        rtunnel_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, False)
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_wait_for_terminal_entry_point",
        lambda **kwargs: events.append(("entry", kwargs["api_term_created"])),
    )
    monkeypatch.setattr(rtunnel_module, "_dismiss_terminal_dialog_once", lambda **_kwargs: False)
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_dom_fallback",
        lambda **kwargs: events.append(("fallback", kwargs["api_term_created"])) or True,
    )

    assert _open_or_create_terminal(context=object(), page=object(), lab_frame=object()) is True
    assert ("entry", False) in events
    assert ("fallback", False) in events


def test_open_or_create_terminal_returns_false_when_dom_fallback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"tab_click": 0}

    monkeypatch.setattr(
        rtunnel_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, False)
    )
    monkeypatch.setattr(rtunnel_module, "_wait_for_terminal_entry_point", lambda **_kwargs: None)
    monkeypatch.setattr(rtunnel_module, "_dismiss_terminal_dialog_once", lambda **_kwargs: False)
    monkeypatch.setattr(rtunnel_module, "_open_terminal_via_dom_fallback", lambda **_kwargs: False)
    monkeypatch.setattr(
        rtunnel_module,
        "_click_terminal_tab",
        lambda *_args, **_kwargs: calls.__setitem__("tab_click", calls["tab_click"] + 1) or True,
    )

    assert _open_or_create_terminal(context=object(), page=object(), lab_frame=object()) is False
    assert calls["tab_click"] == 0


def test_open_or_create_terminal_returns_true_when_api_recovery_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"entry": 0, "fallback": 0}

    monkeypatch.setattr(
        rtunnel_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, True)
    )
    monkeypatch.setattr(rtunnel_module, "_recover_api_terminal_surface", lambda **_kwargs: True)
    monkeypatch.setattr(
        rtunnel_module,
        "_wait_for_terminal_entry_point",
        lambda **_kwargs: calls.__setitem__("entry", calls["entry"] + 1),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: calls.__setitem__("fallback", calls["fallback"] + 1) or True,
    )

    assert _open_or_create_terminal(context=object(), page=object(), lab_frame=object()) is True
    assert calls["entry"] == 0
    assert calls["fallback"] == 0


def test_open_terminal_via_rest_api_handles_playwright_navigation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rtunnel_module, "_create_terminal_via_api", lambda *_args, **_kwargs: "1")

    class _Frame:
        url = "https://nb.example.com/lab"

        def goto(self, *_args, **_kwargs) -> None:
            raise rtunnel_module.PlaywrightError("navigation failed")

    terminal_ready, api_term_created = rtunnel_module._open_terminal_via_rest_api(  # noqa: SLF001
        context=object(),
        page=object(),
        lab_frame=_Frame(),
    )
    assert terminal_ready is False
    assert api_term_created is True


def test_recover_api_terminal_surface_waits_for_menu_before_file_menu_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"file_menu": 0}

    monkeypatch.setattr(rtunnel_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        rtunnel_module,
        "_wait_for_terminal_surface_progressive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        rtunnel_module, "_wait_for_file_menu_ready", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_from_file_menu",
        lambda *_args, **_kwargs: calls.__setitem__("file_menu", calls["file_menu"] + 1) or True,
    )

    assert (
        rtunnel_module._recover_api_terminal_surface(  # noqa: SLF001
            lab_frame=object(),
            page=object(),
        )
        is False
    )
    assert calls["file_menu"] == 0


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
