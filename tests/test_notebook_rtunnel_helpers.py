"""Tests for REST API terminal creation, batch script, and _StepTimer helpers."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from inspire.platform.web.browser_api import rtunnel as rtunnel_module
from inspire.platform.web.browser_api.rtunnel import terminal as terminal_module
from inspire.platform.web.browser_api.rtunnel import upload as upload_module
from inspire.platform.web.browser_api.rtunnel import (
    _CONTENTS_API_RTUNNEL_FILENAME,
    SSH_SERVER_MISSING_MARKER,
    SSHD_MISSING_MARKER,
    _StepTimer,
    _attach_ws_output_listener,
    _build_batch_setup_script,
    build_rtunnel_setup_commands,
    _build_terminal_websocket_url,
    _compute_rtunnel_hash,
    _create_terminal_via_api,
    _delete_terminal_via_api,
    _detach_ws_output_listener,
    _download_rtunnel_locally,
    _extract_jupyter_token,
    _focus_terminal_input,
    _jupyter_server_base,
    _open_or_create_terminal,
    _poll_ws_capture,
    _resolve_rtunnel_binary,
    _rtunnel_matches_on_notebook,
    _send_setup_command_via_terminal_ws,
    _send_terminal_command_via_websocket,
    _upload_rtunnel_hash_sidecar,
    _upload_rtunnel_via_contents_api,
    _verify_terminal_focus,
    _wait_for_setup_completion,
    _wait_for_terminal_surface,
    _wait_for_terminal_surface_progressive,
    _wait_for_ws_capture,
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
# _delete_terminal_via_api
# ---------------------------------------------------------------------------


class _DummyDeleteRequest:
    def __init__(self, status: int) -> None:
        self.status = status
        self.calls: list[tuple[str, dict | None, int]] = []

    def delete(self, url: str, headers: dict | None = None, timeout: int = 0) -> _DummyResponse:
        self.calls.append((url, headers, timeout))
        return _DummyResponse(self.status, {})


class _DummyDeleteContext:
    def __init__(self, request: _DummyDeleteRequest, cookies: list[dict] | None = None) -> None:
        self.request = request
        self._cookies = cookies or []

    def cookies(self) -> list[dict]:
        return self._cookies


def test_delete_terminal_via_api_success_with_xsrf_header() -> None:
    request = _DummyDeleteRequest(status=204)
    ctx = _DummyDeleteContext(
        request,
        cookies=[{"name": "_xsrf", "value": "token-123"}],
    )

    assert (
        _delete_terminal_via_api(ctx, lab_url="https://nb.example.com/lab", term_name="7") is True
    )
    assert len(request.calls) == 1
    assert request.calls[0][0] == "https://nb.example.com/api/terminals/7"
    assert request.calls[0][1] == {"X-XSRFToken": "token-123"}


def test_delete_terminal_via_api_404_is_treated_as_success() -> None:
    request = _DummyDeleteRequest(status=404)
    ctx = _DummyDeleteContext(request)

    assert (
        _delete_terminal_via_api(ctx, lab_url="https://nb.example.com/lab", term_name="7") is True
    )


def test_delete_terminal_via_api_failure_status() -> None:
    request = _DummyDeleteRequest(status=500)
    ctx = _DummyDeleteContext(request)

    assert (
        _delete_terminal_via_api(ctx, lab_url="https://nb.example.com/lab", term_name="7") is False
    )


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


def test_send_setup_command_via_terminal_ws_cleans_up_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []

    class _Frame:
        url = "https://nb.example.com/lab"

    monkeypatch.setattr(terminal_module, "_create_terminal_via_api", lambda *_a, **_k: "term-1")
    monkeypatch.setattr(
        terminal_module,
        "_build_terminal_websocket_url",
        lambda _url, _term: "wss://nb.example.com/terminals/websocket/term-1",
    )
    monkeypatch.setattr(
        terminal_module,
        "_send_terminal_command_via_websocket",
        lambda *_a, **_k: events.append(("send", "ok")) or True,
    )
    monkeypatch.setattr(
        terminal_module,
        "_delete_terminal_via_api",
        lambda _ctx, *, lab_url, term_name: events.append(("delete", f"{lab_url}|{term_name}"))
        or True,
    )

    assert (
        _send_setup_command_via_terminal_ws(context=object(), lab_frame=_Frame(), batch_cmd="echo")
        is True
    )
    assert ("send", "ok") in events
    assert ("delete", "https://nb.example.com/lab|term-1") in events


def test_send_setup_command_via_terminal_ws_cleans_up_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Frame:
        url = "https://nb.example.com/lab"

    monkeypatch.setattr(terminal_module, "_create_terminal_via_api", lambda *_a, **_k: "term-2")
    monkeypatch.setattr(
        terminal_module,
        "_build_terminal_websocket_url",
        lambda _url, _term: "wss://nb.example.com/terminals/websocket/term-2",
    )
    monkeypatch.setattr(
        terminal_module, "_send_terminal_command_via_websocket", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        terminal_module,
        "_delete_terminal_via_api",
        lambda *_a, **_k: events.append("deleted") or True,
    )

    assert (
        _send_setup_command_via_terminal_ws(
            context=object(),
            lab_frame=_Frame(),
            batch_cmd="echo",
        )
        is False
    )
    assert events == ["deleted"]


def test_send_setup_command_via_terminal_ws_returns_false_when_terminal_create_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal_module, "_create_terminal_via_api", lambda *_a, **_k: None)

    assert (
        _send_setup_command_via_terminal_ws(
            context=object(),
            lab_frame=type("_Frame", (), {"url": "https://nb.example.com/lab"})(),
            batch_cmd="echo",
        )
        is False
    )


# ---------------------------------------------------------------------------
# terminal surface and focus helpers
# ---------------------------------------------------------------------------


class _LocatorStub:
    def __init__(
        self,
        *,
        count: int = 0,
        wait_ok: bool = False,
        visible: bool = False,
    ) -> None:
        self._count = count
        self._wait_ok = wait_ok
        self._visible = visible
        self.first = self
        self.wait_calls: list[tuple[str, int]] = []
        self.click_calls: list[int] = []

    def count(self) -> int:
        return self._count

    def is_visible(self, timeout: int = 0) -> bool:
        return self._visible

    def wait_for(self, *, state: str, timeout: int) -> None:
        self.wait_calls.append((state, timeout))
        if not self._wait_ok:
            raise TimeoutError("not ready")

    def click(self, timeout: int = 0, force: bool = False) -> None:
        self.click_calls.append(timeout)


class _FrameStub:
    def __init__(
        self,
        selectors: dict[str, _LocatorStub],
        evaluate_results: list[object] | None = None,
    ) -> None:
        self._selectors = selectors
        self._evaluate_results = list(evaluate_results) if evaluate_results else []
        self._evaluate_idx = 0

    def locator(self, selector: str) -> _LocatorStub:
        return self._selectors.setdefault(selector, _LocatorStub())

    def evaluate(self, expression: str) -> object:
        if self._evaluate_idx < len(self._evaluate_results):
            result = self._evaluate_results[self._evaluate_idx]
            self._evaluate_idx += 1
            return result
        return None


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

    monkeypatch.setattr(terminal_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(terminal_module, "_wait_for_terminal_surface", fake_wait_surface)
    monkeypatch.setattr(terminal_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)

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

    monkeypatch.setattr(terminal_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(
        terminal_module, "_wait_for_terminal_surface", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(terminal_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)

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


def test_verify_terminal_focus_true() -> None:
    frame = _FrameStub({}, evaluate_results=["textarea", "xterm-helper-textarea"])
    assert _verify_terminal_focus(frame) is True


def test_verify_terminal_focus_wrong_tag() -> None:
    frame = _FrameStub({}, evaluate_results=["div", "xterm-helper-textarea"])
    assert _verify_terminal_focus(frame) is False


def test_verify_terminal_focus_wrong_class() -> None:
    frame = _FrameStub({}, evaluate_results=["textarea", "some-other-class"])
    assert _verify_terminal_focus(frame) is False


def test_verify_terminal_focus_exception() -> None:
    class _BrokenFrame:
        def evaluate(self, _expr: str) -> object:
            raise RuntimeError("frame detached")

    assert _verify_terminal_focus(_BrokenFrame()) is False


def test_focus_terminal_input_succeeds_via_xterm_container() -> None:
    """Focus via .xterm container click when it's visible and focus verifies."""
    xterm = _LocatorStub(count=1, visible=True)
    textarea = _LocatorStub(wait_ok=True)
    # evaluate returns: tagName="textarea", className="xterm-helper-textarea"
    frame = _FrameStub(
        {".xterm": xterm, "textarea.xterm-helper-textarea": textarea},
        evaluate_results=["textarea", "xterm-helper-textarea"],
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is True
    assert len(xterm.click_calls) == 1
    assert 40 in page.wait_calls


def test_focus_terminal_input_succeeds_via_force_click_textarea() -> None:
    """When .xterm click doesn't verify focus, atomic JS focus path succeeds."""
    # .xterm verify fails (2 evaluates), then atomic JS returns True (1 evaluate).
    textarea = _LocatorStub(wait_ok=True)
    frame = _FrameStub(
        {".xterm": _LocatorStub(count=1), "textarea.xterm-helper-textarea": textarea},
        evaluate_results=["div", "", True],
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is True


def test_focus_terminal_input_returns_false_when_focus_never_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns False when both focus strategies fail on all passes."""
    monkeypatch.setattr(terminal_module, "_click_terminal_tab", lambda *_a, **_kw: False)

    # Per pass: .xterm verify consumes 2 evaluates, atomic JS consumes 1.
    # "div", "" → verify fails; False → atomic JS returns falsy.
    textarea = _LocatorStub(wait_ok=True)
    frame = _FrameStub(
        {".xterm": _LocatorStub(count=1), "textarea.xterm-helper-textarea": textarea},
        evaluate_results=["div", "", False] * 5,
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is False


def test_focus_terminal_input_succeeds_via_atomic_js_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic JS focus succeeds when .xterm click path fails."""
    monkeypatch.setattr(terminal_module, "_click_terminal_tab", lambda *_a, **_kw: False)

    # .xterm count=0 (Try 1 skipped), atomic JS returns True
    textarea = _LocatorStub(wait_ok=True)
    frame = _FrameStub(
        {".xterm": _LocatorStub(count=0), "textarea.xterm-helper-textarea": textarea},
        evaluate_results=[True],
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is True


def test_focus_terminal_input_returns_false_when_textarea_not_attached() -> None:
    """Returns False immediately when xterm textarea hasn't been created yet."""
    textarea = _LocatorStub(wait_ok=False)  # textarea not yet attached
    xterm = _LocatorStub(count=1, wait_ok=True)
    frame = _FrameStub(
        {".xterm": xterm, "textarea.xterm-helper-textarea": textarea},
        evaluate_results=["textarea", "xterm-helper-textarea"],
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is False
    # Should not have attempted any clicks (gate failed before retry loop)
    assert len(xterm.click_calls) == 0


def test_focus_terminal_input_returns_false_when_unavailable() -> None:
    frame = _FrameStub({})
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is False


def test_open_or_create_terminal_returns_early_when_api_path_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"recover": 0, "entry": 0, "fallback": 0}

    monkeypatch.setattr(
        terminal_module,
        "_open_terminal_via_rest_api",
        lambda **_kwargs: (True, True, "api-1"),
    )
    monkeypatch.setattr(
        terminal_module,
        "_recover_api_terminal_surface",
        lambda **_kwargs: calls.__setitem__("recover", calls["recover"] + 1) or False,
    )
    monkeypatch.setattr(
        terminal_module,
        "_wait_for_terminal_entry_point",
        lambda **_kwargs: calls.__setitem__("entry", calls["entry"] + 1),
    )
    monkeypatch.setattr(
        terminal_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: calls.__setitem__("fallback", calls["fallback"] + 1) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is True
    assert term_name == "api-1"
    assert calls["recover"] == 0
    assert calls["entry"] == 0
    assert calls["fallback"] == 0


def test_open_or_create_terminal_uses_dom_fallback_after_api_recovery_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(
        terminal_module,
        "_open_terminal_via_rest_api",
        lambda **_kwargs: (False, True, "api-2"),
    )
    monkeypatch.setattr(
        terminal_module,
        "_recover_api_terminal_surface",
        lambda **_kwargs: False,
    )

    def fake_wait_entry(*, lab_frame, api_term_created: bool) -> None:  # noqa: ANN001
        events.append(("entry", api_term_created))

    monkeypatch.setattr(terminal_module, "_wait_for_terminal_entry_point", fake_wait_entry)
    monkeypatch.setattr(
        terminal_module,
        "_dismiss_terminal_dialog_once",
        lambda **kwargs: events.append(("dismiss", kwargs["settle_ms"])) or False,
    )
    monkeypatch.setattr(
        terminal_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        terminal_module,
        "_click_terminal_tab",
        lambda *_args, **kwargs: events.append(("tab_click", kwargs["settle_ms"])) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is True
    assert term_name == "api-2"
    assert ("entry", True) in events
    assert ("tab_click", 80) in events


def test_open_or_create_terminal_handles_api_full_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(
        terminal_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, False, None)
    )
    monkeypatch.setattr(
        terminal_module,
        "_wait_for_terminal_entry_point",
        lambda **kwargs: events.append(("entry", kwargs["api_term_created"])),
    )
    monkeypatch.setattr(terminal_module, "_dismiss_terminal_dialog_once", lambda **_kwargs: False)
    monkeypatch.setattr(
        terminal_module,
        "_open_terminal_via_dom_fallback",
        lambda **kwargs: events.append(("fallback", kwargs["api_term_created"])) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is True
    assert term_name is None
    assert ("entry", False) in events
    assert ("fallback", False) in events


def test_open_or_create_terminal_returns_false_when_dom_fallback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"tab_click": 0}

    monkeypatch.setattr(
        terminal_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, False, None)
    )
    monkeypatch.setattr(terminal_module, "_wait_for_terminal_entry_point", lambda **_kwargs: None)
    monkeypatch.setattr(terminal_module, "_dismiss_terminal_dialog_once", lambda **_kwargs: False)
    monkeypatch.setattr(terminal_module, "_open_terminal_via_dom_fallback", lambda **_kwargs: False)
    monkeypatch.setattr(
        terminal_module,
        "_click_terminal_tab",
        lambda *_args, **_kwargs: calls.__setitem__("tab_click", calls["tab_click"] + 1) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is False
    assert term_name is None
    assert calls["tab_click"] == 0


def test_open_or_create_terminal_returns_true_when_api_recovery_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"entry": 0, "fallback": 0}

    monkeypatch.setattr(
        terminal_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, True, "api-5")
    )
    monkeypatch.setattr(terminal_module, "_recover_api_terminal_surface", lambda **_kwargs: True)
    monkeypatch.setattr(
        terminal_module,
        "_wait_for_terminal_entry_point",
        lambda **_kwargs: calls.__setitem__("entry", calls["entry"] + 1),
    )
    monkeypatch.setattr(
        terminal_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: calls.__setitem__("fallback", calls["fallback"] + 1) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is True
    assert term_name == "api-5"
    assert calls["entry"] == 0
    assert calls["fallback"] == 0


def test_open_terminal_via_rest_api_handles_playwright_navigation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal_module, "_create_terminal_via_api", lambda *_args, **_kwargs: "1")

    class _Frame:
        url = "https://nb.example.com/lab"

        def goto(self, *_args, **_kwargs) -> None:
            raise rtunnel_module.PlaywrightError("navigation failed")

    terminal_ready, api_term_created, term_name = (
        terminal_module._open_terminal_via_rest_api(  # noqa: SLF001
            context=object(),
            page=object(),
            lab_frame=_Frame(),
        )
    )
    assert terminal_ready is False
    assert api_term_created is True
    assert term_name == "1"


def test_recover_api_terminal_surface_waits_for_menu_before_file_menu_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"file_menu": 0}

    monkeypatch.setattr(terminal_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        terminal_module,
        "_wait_for_terminal_surface_progressive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        terminal_module, "_wait_for_file_menu_ready", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        terminal_module,
        "_open_terminal_from_file_menu",
        lambda *_args, **_kwargs: calls.__setitem__("file_menu", calls["file_menu"] + 1) or True,
    )

    assert (
        terminal_module._recover_api_terminal_surface(  # noqa: SLF001
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

    assert result.startswith("cat <<'__INSPIRE_RTUNNEL_B64__' | base64 -d | bash\n")
    assert result.endswith("\n__INSPIRE_RTUNNEL_B64__")

    lines = result.splitlines()
    assert lines[0] == "cat <<'__INSPIRE_RTUNNEL_B64__' | base64 -d | bash"
    assert lines[-1] == "__INSPIRE_RTUNNEL_B64__"

    b64_payload = "".join(lines[1:-1])
    decoded = base64.b64decode(b64_payload).decode()

    for cmd in commands:
        assert cmd in decoded

    decoded_lines = decoded.strip().split("\n")
    assert decoded_lines == commands


def test_build_batch_setup_script_empty() -> None:
    result = _build_batch_setup_script([])
    lines = result.splitlines()
    assert lines[0] == "cat <<'__INSPIRE_RTUNNEL_B64__' | base64 -d | bash"
    assert lines[-1] == "__INSPIRE_RTUNNEL_B64__"
    b64_payload = "".join(lines[1:-1])
    decoded = base64.b64decode(b64_payload).decode()
    assert decoded == "\n"


# ---------------------------------------------------------------------------
# build_rtunnel_setup_commands
# ---------------------------------------------------------------------------


def test_build_rtunnel_setup_commands_gates_network_calls_on_inet_probe() -> None:
    from inspire.config.ssh_runtime import SshRuntimeConfig

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=SshRuntimeConfig(),
    )
    script = "\n".join(commands)

    assert "_INET=0; timeout 3 bash -c 'exec 3<>/dev/tcp/archive.ubuntu.com/80'" in script
    assert 'if [ ! -x "$RTUNNEL_BIN" ] && [ "$_INET" = 1 ]; then curl -fsSL ' in script
    assert (
        "timeout 30 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=10 " "update -qq"
    ) in script
    assert "timeout 30 apt-get install -y -qq openssh-server" in script


def test_build_rtunnel_setup_commands_apt_mirror_path_skips_curl_and_time_bounds_dropbear() -> None:
    from inspire.config.ssh_runtime import SshRuntimeConfig

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=SshRuntimeConfig(
            apt_mirror_url="http://mirror.example/ubuntu/",
            rtunnel_bin="/shared/bin/rtunnel",
        ),
    )
    script = "\n".join(commands)

    assert "APT_MIRROR_URL=http://mirror.example/ubuntu/" in script
    assert "timeout 60 apt-get update -qq >/dev/null 2>&1" in script
    assert "timeout 60 apt-get install -y -qq dropbear-bin >/dev/null 2>&1 || true" in script
    assert "no curl fallback for offline notebooks" in script
    assert "curl -fsSL" not in script


def test_build_rtunnel_setup_commands_repository_root_mirror_is_supported() -> None:
    from inspire.config.ssh_runtime import SshRuntimeConfig

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=SshRuntimeConfig(
            apt_mirror_url="http://mirror.example/repository/",
            rtunnel_bin="/shared/bin/rtunnel",
        ),
    )
    script = "\n".join(commands)

    assert "APT_MIRROR_URL=http://mirror.example/repository/" in script
    assert 'MIRROR_URL="${APT_MIRROR_URL%/}"' in script
    assert '*/repository) MIRROR_URL="$MIRROR_URL/$MIRROR_DISTRO" ;;' in script
    assert 'echo "deb $MIRROR_URL $CODENAME $MIRROR_COMPONENTS" ' in script


def test_build_rtunnel_setup_commands_sshd_deb_dir_stays_on_openssh_path() -> None:
    from inspire.config.ssh_runtime import SshRuntimeConfig

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=SshRuntimeConfig(
            sshd_deb_dir="/shared/sshd-debs",
        ),
    )
    script = "\n".join(commands)

    assert "SSHD_DEB_DIR=/shared/sshd-debs" in script
    assert 'dpkg -i "$SSHD_DEB_DIR"/*.deb >/dev/null 2>&1 || true;' in script
    assert "dropbear-bin" not in script
    assert SSHD_MISSING_MARKER in script
    assert SSH_SERVER_MISSING_MARKER in script
    assert (
        'ss -ltnp 2>/dev/null | grep -Eq "127\\\\.0\\\\.0\\\\.1:${SSH_PORT}[[:space:]]|' in script
    )
    assert "[s]shd: .*-p ${SSH_PORT}([[:space:]]|$)|" in script


def test_build_rtunnel_setup_commands_uses_configured_rtunnel_bin_in_place() -> None:
    from inspire.config.ssh_runtime import SshRuntimeConfig

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=SshRuntimeConfig(
            rtunnel_bin="/shared/bin/rtunnel",
        ),
    )
    script = "\n".join(commands)

    assert 'if [ -x "$RTUNNEL_BIN_PATH" ]; then RTUNNEL_BIN="$RTUNNEL_BIN_PATH"; ' in script
    assert 'nohup "$RTUNNEL_BIN" "$SSH_PORT" "$PORT" ' in script
    assert 'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x "$RTUNNEL_BIN" ] ' in script


# ---------------------------------------------------------------------------
# _wait_for_setup_completion
# ---------------------------------------------------------------------------


class _TimerStub:
    def __init__(self) -> None:
        self.labels: list[str] = []

    def mark(self, label: str) -> None:
        self.labels.append(label)


class _WaitPageStub:
    def __init__(self) -> None:
        self.wait_calls: list[int] = []

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)


def test_wait_for_setup_completion_uses_short_settle_for_ws_path() -> None:
    page = _WaitPageStub()
    timer = _TimerStub()

    _wait_for_setup_completion(page=page, setup_confirmed=True, timer=timer)

    assert page.wait_calls == [500]
    assert timer.labels == ["wait_marker"]


def test_wait_for_setup_completion_uses_longer_settle_for_browser_path() -> None:
    page = _WaitPageStub()
    timer = _TimerStub()

    _wait_for_setup_completion(page=page, setup_confirmed=False, timer=timer)

    assert page.wait_calls == [3000]
    assert timer.labels == ["wait_marker"]


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


# ---------------------------------------------------------------------------
# _upload_rtunnel_via_contents_api
# ---------------------------------------------------------------------------


class _DummyUploadResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _DummyUploadRequest:
    def __init__(self, response: _DummyUploadResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, dict | None, dict | None, int]] = []

    def put(
        self,
        url: str,
        headers: dict | None = None,
        data: dict | None = None,
        timeout: int = 0,
    ) -> _DummyUploadResponse:
        self.calls.append((url, headers, data, timeout))
        return self._response


class _DummyUploadContext:
    def __init__(self, request: _DummyUploadRequest) -> None:
        self.request = request

    def cookies(self) -> list[dict]:
        return []


def test_upload_rtunnel_via_contents_api_success(tmp_path: Path) -> None:
    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"\x7fELF_test_binary")

    resp = _DummyUploadResponse(201)
    req = _DummyUploadRequest(resp)
    ctx = _DummyUploadContext(req)

    result = _upload_rtunnel_via_contents_api(ctx, "https://nb.example.com/lab", binary)
    assert result is True
    assert len(req.calls) == 1

    url, _headers, data, timeout = req.calls[0]
    assert url == f"https://nb.example.com/api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}"
    assert data["type"] == "file"
    assert data["format"] == "base64"
    # Verify the payload round-trips
    import base64 as _b64

    assert _b64.b64decode(data["content"]) == b"\x7fELF_test_binary"
    assert timeout == 30000


def test_upload_rtunnel_via_contents_api_failure_status(tmp_path: Path) -> None:
    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"\x7fELF")

    resp = _DummyUploadResponse(500)
    req = _DummyUploadRequest(resp)
    ctx = _DummyUploadContext(req)

    result = _upload_rtunnel_via_contents_api(ctx, "https://nb.example.com/lab", binary)
    assert result is False


def test_upload_rtunnel_via_contents_api_missing_binary() -> None:
    resp = _DummyUploadResponse(201)
    req = _DummyUploadRequest(resp)
    ctx = _DummyUploadContext(req)

    result = _upload_rtunnel_via_contents_api(
        ctx, "https://nb.example.com/lab", Path("/nonexistent/rtunnel")
    )
    assert result is False
    assert len(req.calls) == 0


def test_upload_rtunnel_via_contents_api_network_error(tmp_path: Path) -> None:
    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"\x7fELF")

    class _BrokenUploadRequest:
        def put(self, url: str, **kwargs: object) -> None:
            raise ConnectionError("network failure")

    ctx = _DummyUploadContext(_BrokenUploadRequest())  # type: ignore[arg-type]

    result = _upload_rtunnel_via_contents_api(ctx, "https://nb.example.com/lab", binary)
    assert result is False


# ---------------------------------------------------------------------------
# _download_rtunnel_locally
# ---------------------------------------------------------------------------


def test_download_rtunnel_locally_success(tmp_path: Path) -> None:
    import tarfile

    # Build a valid .tar.gz containing a file named "rtunnel"
    binary_content = b"\x7fELF_fake_rtunnel"
    tar_path = tmp_path / "rtunnel.tar.gz"
    member_path = tmp_path / "rtunnel"
    member_path.write_bytes(binary_content)
    with tarfile.open(str(tar_path), "w:gz") as tar:
        tar.add(str(member_path), arcname="rtunnel")

    dest = tmp_path / "output" / "rtunnel"

    import shutil
    import urllib.request

    original_urlretrieve = urllib.request.urlretrieve

    def fake_urlretrieve(url: str, filename: str) -> tuple:
        shutil.copy2(str(tar_path), filename)
        return (filename, None)

    urllib.request.urlretrieve = fake_urlretrieve  # type: ignore[assignment]
    try:
        result = _download_rtunnel_locally("https://example.com/rtunnel.tar.gz", dest)
    finally:
        urllib.request.urlretrieve = original_urlretrieve  # type: ignore[assignment]

    assert result is True
    assert dest.exists()
    assert dest.read_bytes() == binary_content
    assert dest.stat().st_mode & 0o755


def test_download_rtunnel_locally_network_error(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    dest = tmp_path / "rtunnel"

    original_urlretrieve = urllib.request.urlretrieve

    def broken_urlretrieve(url: str, filename: str) -> None:
        raise urllib.error.URLError("network failure")

    urllib.request.urlretrieve = broken_urlretrieve  # type: ignore[assignment]
    try:
        result = _download_rtunnel_locally("https://example.com/rtunnel.tar.gz", dest)
    finally:
        urllib.request.urlretrieve = original_urlretrieve  # type: ignore[assignment]

    assert result is False
    assert not dest.exists()


def test_download_rtunnel_locally_no_rtunnel_in_archive(tmp_path: Path) -> None:
    import tarfile

    # Build a .tar.gz with no file named "rtunnel"
    tar_path = tmp_path / "bad.tar.gz"
    other_file = tmp_path / "other.txt"
    other_file.write_text("not rtunnel")
    with tarfile.open(str(tar_path), "w:gz") as tar:
        tar.add(str(other_file), arcname="other.txt")

    dest = tmp_path / "output" / "rtunnel"

    import shutil
    import urllib.request

    original_urlretrieve = urllib.request.urlretrieve

    def fake_urlretrieve(url: str, filename: str) -> tuple:
        shutil.copy2(str(tar_path), filename)
        return (filename, None)

    urllib.request.urlretrieve = fake_urlretrieve  # type: ignore[assignment]
    try:
        result = _download_rtunnel_locally("https://example.com/rtunnel.tar.gz", dest)
    finally:
        urllib.request.urlretrieve = original_urlretrieve  # type: ignore[assignment]

    assert result is False
    assert not dest.exists()


# ---------------------------------------------------------------------------
# _compute_rtunnel_hash
# ---------------------------------------------------------------------------


def test_compute_rtunnel_hash(tmp_path: Path) -> None:
    import hashlib

    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"\x7fELF_test_binary")
    expected = hashlib.sha256(b"\x7fELF_test_binary").hexdigest()
    assert _compute_rtunnel_hash(binary) == expected


def test_compute_rtunnel_hash_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_file"
    assert _compute_rtunnel_hash(missing) is None


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
def test_compute_rtunnel_hash_permission_error(tmp_path: Path) -> None:
    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o000)
    try:
        assert _compute_rtunnel_hash(binary) is None
    finally:
        binary.chmod(0o644)


# ---------------------------------------------------------------------------
# _rtunnel_matches_on_notebook
# ---------------------------------------------------------------------------


class _MatchGetResponse:
    def __init__(self, status: int, data: dict | None = None) -> None:
        self.status = status
        self._data = data

    def json(self) -> dict:
        return self._data or {}


class _MatchGetRequest:
    """Fake request that returns different responses per URL substring."""

    def __init__(self, responses: dict[str, _MatchGetResponse]) -> None:
        self._responses = responses

    def get(self, url: str, timeout: int = 0) -> _MatchGetResponse:
        for key, resp in self._responses.items():
            if key in url:
                return resp
        return _MatchGetResponse(500)


class _MatchContext:
    def __init__(self, request: _MatchGetRequest) -> None:
        self.request = request

    def cookies(self) -> list[dict]:
        return []


def test_rtunnel_matches_on_notebook_hit() -> None:
    import base64 as _b64

    local_hash = "abc123"
    sidecar_b64 = _b64.b64encode(local_hash.encode()).decode()
    ctx = _MatchContext(
        _MatchGetRequest(
            {
                f"contents/{_CONTENTS_API_RTUNNEL_FILENAME}?content=0": _MatchGetResponse(200),
                f"contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256": _MatchGetResponse(
                    200, {"content": sidecar_b64}
                ),
            }
        )
    )
    assert _rtunnel_matches_on_notebook(ctx, "https://nb.example.com/lab", local_hash) is True


def test_rtunnel_matches_on_notebook_binary_missing() -> None:
    ctx = _MatchContext(
        _MatchGetRequest(
            {
                f"contents/{_CONTENTS_API_RTUNNEL_FILENAME}?content=0": _MatchGetResponse(404),
            }
        )
    )
    assert _rtunnel_matches_on_notebook(ctx, "https://nb.example.com/lab", "abc") is False


def test_rtunnel_matches_on_notebook_hash_mismatch() -> None:
    import base64 as _b64

    sidecar_b64 = _b64.b64encode(b"old_hash").decode()
    ctx = _MatchContext(
        _MatchGetRequest(
            {
                f"contents/{_CONTENTS_API_RTUNNEL_FILENAME}?content=0": _MatchGetResponse(200),
                f"contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256": _MatchGetResponse(
                    200, {"content": sidecar_b64}
                ),
            }
        )
    )
    assert _rtunnel_matches_on_notebook(ctx, "https://nb.example.com/lab", "new_hash") is False


def test_rtunnel_matches_on_notebook_sidecar_missing() -> None:
    ctx = _MatchContext(
        _MatchGetRequest(
            {
                f"contents/{_CONTENTS_API_RTUNNEL_FILENAME}?content=0": _MatchGetResponse(200),
                f"contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256": _MatchGetResponse(404),
            }
        )
    )
    assert _rtunnel_matches_on_notebook(ctx, "https://nb.example.com/lab", "abc") is False


def test_rtunnel_matches_on_notebook_error() -> None:
    class _BrokenGetRequest:
        def get(self, url: str, timeout: int = 0) -> None:
            raise ConnectionError("network failure")

    ctx = _MatchContext(_BrokenGetRequest())  # type: ignore[arg-type]
    assert _rtunnel_matches_on_notebook(ctx, "https://nb.example.com/lab", "abc") is False


# ---------------------------------------------------------------------------
# _upload_rtunnel_hash_sidecar
# ---------------------------------------------------------------------------


def test_upload_rtunnel_hash_sidecar() -> None:
    import base64 as _b64

    resp = _DummyUploadResponse(201)
    req = _DummyUploadRequest(resp)
    ctx = _DummyUploadContext(req)

    result = _upload_rtunnel_hash_sidecar(ctx, "https://nb.example.com/lab", "deadbeef")
    assert result is True
    assert len(req.calls) == 1

    url, _headers, data, timeout = req.calls[0]
    assert url == f"https://nb.example.com/api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256"
    assert data["type"] == "file"
    assert data["format"] == "base64"
    assert _b64.b64decode(data["content"]).decode("ascii") == "deadbeef"
    assert timeout == 5000


# ---------------------------------------------------------------------------
# _resolve_rtunnel_binary
# ---------------------------------------------------------------------------


class _ResolveContext:
    """Minimal Playwright browser-context stub for _resolve_rtunnel_binary tests."""

    def __init__(self) -> None:
        self.request = _MatchGetRequest({})

    def cookies(self) -> list[dict]:
        return []


def test_resolve_rtunnel_binary_configured_hash_match(tmp_path, monkeypatch):
    """rtunnel_bin set, local exists, hash matches → return FILENAME, no upload."""
    from inspire.config.ssh_runtime import SshRuntimeConfig

    local_bin = tmp_path / ".local" / "bin" / "rtunnel"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_bytes(b"\x7fELF_rtunnel")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    ssh_rt = SshRuntimeConfig(rtunnel_bin="/shared/bin/rtunnel")
    ctx = _ResolveContext()

    monkeypatch.setattr(upload_module, "_compute_rtunnel_hash", lambda _p: "aaa111")
    monkeypatch.setattr(
        upload_module,
        "_rtunnel_matches_on_notebook",
        lambda _ctx, _url, _h: True,
    )
    upload_called = []
    monkeypatch.setattr(
        upload_module,
        "_upload_rtunnel_via_contents_api",
        lambda *a, **kw: upload_called.append(1) or True,
    )

    result = _resolve_rtunnel_binary(
        context=ctx, lab_url="https://nb.example.com/lab", ssh_runtime=ssh_rt
    )
    assert result == _CONTENTS_API_RTUNNEL_FILENAME
    assert upload_called == []


def test_resolve_rtunnel_binary_configured_hash_mismatch(tmp_path, monkeypatch):
    """rtunnel_bin set, local exists, hash mismatch → return None, no upload."""
    from inspire.config.ssh_runtime import SshRuntimeConfig

    local_bin = tmp_path / ".local" / "bin" / "rtunnel"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_bytes(b"\x7fELF_rtunnel")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    ssh_rt = SshRuntimeConfig(rtunnel_bin="/shared/bin/rtunnel")
    ctx = _ResolveContext()

    hash_calls = []
    monkeypatch.setattr(
        upload_module,
        "_compute_rtunnel_hash",
        lambda _p: (hash_calls.append(1), "aaa111")[1],
    )
    match_calls = []
    monkeypatch.setattr(
        upload_module,
        "_rtunnel_matches_on_notebook",
        lambda _ctx, _url, _h: (match_calls.append(1), False)[1],
    )
    upload_called = []
    monkeypatch.setattr(
        upload_module,
        "_upload_rtunnel_via_contents_api",
        lambda *a, **kw: upload_called.append(1) or True,
    )

    result = _resolve_rtunnel_binary(
        context=ctx, lab_url="https://nb.example.com/lab", ssh_runtime=ssh_rt
    )
    assert result is None
    assert len(hash_calls) == 1
    assert len(match_calls) == 1
    assert upload_called == []


def test_resolve_rtunnel_binary_configured_no_local(tmp_path, monkeypatch):
    """rtunnel_bin set, no local binary → return None, no hash/match/upload calls."""
    from inspire.config.ssh_runtime import SshRuntimeConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    ssh_rt = SshRuntimeConfig(rtunnel_bin="/shared/bin/rtunnel")
    ctx = _ResolveContext()

    hash_calls = []
    monkeypatch.setattr(
        upload_module,
        "_compute_rtunnel_hash",
        lambda _p: hash_calls.append(1) or "aaa111",
    )
    match_calls = []
    monkeypatch.setattr(
        upload_module,
        "_rtunnel_matches_on_notebook",
        lambda _ctx, _url, _h: match_calls.append(1) or True,
    )
    upload_called = []
    monkeypatch.setattr(
        upload_module,
        "_upload_rtunnel_via_contents_api",
        lambda *a, **kw: upload_called.append(1) or True,
    )

    result = _resolve_rtunnel_binary(
        context=ctx, lab_url="https://nb.example.com/lab", ssh_runtime=ssh_rt
    )
    assert result is None
    assert hash_calls == []
    assert match_calls == []
    assert upload_called == []


def test_resolve_rtunnel_binary_not_configured_downloads(tmp_path, monkeypatch):
    """rtunnel_bin not set, no local binary → _download_rtunnel_locally called."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    ctx = _ResolveContext()

    download_calls = []
    monkeypatch.setattr(
        upload_module,
        "_download_rtunnel_locally",
        lambda _url, _dest: (download_calls.append(1), False)[1],
    )
    upload_called = []
    monkeypatch.setattr(
        upload_module,
        "_upload_rtunnel_via_contents_api",
        lambda *a, **kw: upload_called.append(1) or True,
    )

    result = _resolve_rtunnel_binary(
        context=ctx, lab_url="https://nb.example.com/lab", ssh_runtime=None
    )
    assert result is None
    assert len(download_calls) == 1
    assert upload_called == []


# ---------------------------------------------------------------------------
# _resolve_rtunnel_binary — upload policy tests
# ---------------------------------------------------------------------------


def test_resolve_rtunnel_binary_policy_never_returns_none(tmp_path, monkeypatch):
    """policy=never → None, no side-effect calls regardless of local binary."""
    from inspire.config.ssh_runtime import SshRuntimeConfig

    local_bin = tmp_path / ".local" / "bin" / "rtunnel"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_bytes(b"\x7fELF_rtunnel")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    ssh_rt = SshRuntimeConfig(rtunnel_upload_policy="never")
    ctx = _ResolveContext()

    hash_calls = []
    monkeypatch.setattr(
        upload_module, "_compute_rtunnel_hash", lambda _p: hash_calls.append(1) or "aaa111"
    )
    upload_called = []
    monkeypatch.setattr(
        upload_module,
        "_upload_rtunnel_via_contents_api",
        lambda *a, **kw: upload_called.append(1) or True,
    )
    download_calls = []
    monkeypatch.setattr(
        upload_module,
        "_download_rtunnel_locally",
        lambda _url, _dest: (download_calls.append(1), False)[1],
    )

    result = _resolve_rtunnel_binary(
        context=ctx, lab_url="https://nb.example.com/lab", ssh_runtime=ssh_rt
    )
    assert result is None
    assert hash_calls == []
    assert upload_called == []
    assert download_calls == []


def test_resolve_rtunnel_binary_policy_never_ignores_configured_bin(tmp_path, monkeypatch):
    """policy=never + rtunnel_bin configured → still None."""
    from inspire.config.ssh_runtime import SshRuntimeConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    ssh_rt = SshRuntimeConfig(
        rtunnel_bin="/shared/bin/rtunnel",
        rtunnel_upload_policy="never",
    )
    ctx = _ResolveContext()

    hash_calls = []
    monkeypatch.setattr(
        upload_module, "_compute_rtunnel_hash", lambda _p: hash_calls.append(1) or "aaa111"
    )

    result = _resolve_rtunnel_binary(
        context=ctx, lab_url="https://nb.example.com/lab", ssh_runtime=ssh_rt
    )
    assert result is None
    assert hash_calls == []


def test_resolve_rtunnel_binary_policy_always_forces_upload(tmp_path, monkeypatch):
    """policy=always + rtunnel_bin + local binary → upload called."""
    from inspire.config.ssh_runtime import SshRuntimeConfig

    local_bin = tmp_path / ".local" / "bin" / "rtunnel"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_bytes(b"\x7fELF_rtunnel")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    ssh_rt = SshRuntimeConfig(
        rtunnel_bin="/shared/bin/rtunnel",
        rtunnel_upload_policy="always",
    )
    ctx = _ResolveContext()

    monkeypatch.setattr(upload_module, "_compute_rtunnel_hash", lambda _p: "aaa111")
    monkeypatch.setattr(upload_module, "_rtunnel_matches_on_notebook", lambda _ctx, _url, _h: False)
    upload_called = []
    monkeypatch.setattr(
        upload_module,
        "_upload_rtunnel_via_contents_api",
        lambda *a, **kw: upload_called.append(1) or True,
    )
    monkeypatch.setattr(upload_module, "_upload_rtunnel_hash_sidecar", lambda *a, **kw: True)

    result = _resolve_rtunnel_binary(
        context=ctx, lab_url="https://nb.example.com/lab", ssh_runtime=ssh_rt
    )
    assert result == _CONTENTS_API_RTUNNEL_FILENAME
    assert len(upload_called) == 1


def test_resolve_rtunnel_binary_policy_always_downloads_when_no_local(tmp_path, monkeypatch):
    """policy=always + no local → download attempted."""
    from inspire.config.ssh_runtime import SshRuntimeConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    ssh_rt = SshRuntimeConfig(
        rtunnel_bin="/shared/bin/rtunnel",
        rtunnel_upload_policy="always",
    )
    ctx = _ResolveContext()

    download_calls = []
    monkeypatch.setattr(
        upload_module,
        "_download_rtunnel_locally",
        lambda _url, _dest: (download_calls.append(1), False)[1],
    )

    result = _resolve_rtunnel_binary(
        context=ctx, lab_url="https://nb.example.com/lab", ssh_runtime=ssh_rt
    )
    assert result is None
    assert len(download_calls) == 1


# ---------------------------------------------------------------------------
# _send_terminal_command_via_websocket — error marker detection
# ---------------------------------------------------------------------------


def _make_eval_page(return_value):  # noqa: ANN001, ANN202
    """Create a mock page whose evaluate() returns a fixed value."""

    class _EvalPage:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            return return_value

    return _EvalPage()


def test_send_terminal_command_via_websocket_populates_detected_errors() -> None:
    """When evaluate returns a dict with errors, detected_errors should be populated."""
    page = _make_eval_page({"ok": True, "errors": ["MARKER"]})
    errors: list[str] = []
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
        error_markers=["MARKER"],
        detected_errors=errors,
    )
    assert result is True
    assert errors == ["MARKER"]


def test_send_terminal_command_via_websocket_dict_ok_false() -> None:
    """When evaluate returns a dict with ok=False and no errors, returns False."""
    page = _make_eval_page({"ok": False, "errors": []})
    errors: list[str] = []
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
        detected_errors=errors,
    )
    assert result is False
    assert errors == []


def test_send_terminal_command_via_websocket_dict_ok_false_with_errors() -> None:
    """WS timeout with marker captured: ok=False but errors populated."""
    page = _make_eval_page({"ok": False, "errors": ["MARKER"]})
    errors: list[str] = []
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
        error_markers=["MARKER"],
        detected_errors=errors,
    )
    assert result is False
    assert errors == ["MARKER"]


def test_send_terminal_command_via_websocket_plain_bool_still_works() -> None:
    """Existing mock pages returning plain bool should still work."""
    page = _make_eval_page(True)
    errors: list[str] = []
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
        error_markers=["MARKER"],
        detected_errors=errors,
    )
    assert result is True
    assert errors == []


def test_send_setup_command_via_terminal_ws_propagates_detected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_send_setup_command_via_terminal_ws should propagate detected_errors."""

    class _Frame:
        url = "https://nb.example.com/lab"

    monkeypatch.setattr(terminal_module, "_create_terminal_via_api", lambda *_a, **_k: "term-1")
    monkeypatch.setattr(
        terminal_module,
        "_build_terminal_websocket_url",
        lambda _url, _term: "wss://nb.example.com/terminals/websocket/term-1",
    )

    def fake_send(*_a, **kwargs):  # noqa: ANN202
        detected = kwargs.get("detected_errors")
        if detected is not None:
            detected.append(SSHD_MISSING_MARKER)
        return False

    monkeypatch.setattr(terminal_module, "_send_terminal_command_via_websocket", fake_send)
    monkeypatch.setattr(
        terminal_module,
        "_delete_terminal_via_api",
        lambda *_a, **_k: True,
    )

    errors: list[str] = []
    result = _send_setup_command_via_terminal_ws(
        context=object(),
        lab_frame=_Frame(),
        batch_cmd="echo",
        detected_errors=errors,
    )
    assert result is False
    assert errors == [SSHD_MISSING_MARKER]


# ---------------------------------------------------------------------------
# Phase 1: WS diagnostics logging
# ---------------------------------------------------------------------------


def test_send_terminal_command_logs_diagnostics_on_failure(capsys: pytest.CaptureFixture) -> None:
    """When WS returns {ok: False, diagnostics: {...}}, diagnostics are logged to stderr."""
    diag = {
        "wsConnected": True,
        "promptDetected": False,
        "commandSent": False,
        "stdoutReceived": False,
        "stdoutLen": 0,
        "wsCloseCode": 1006,
        "wsCloseReason": "",
        "elapsed": 5000,
    }
    page = _make_eval_page({"ok": False, "errors": [], "diagnostics": diag})
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
    )
    assert result is False
    captured = capsys.readouterr()
    assert "ws-diagnostics" in captured.err
    assert "wsConnected=True" in captured.err
    assert "promptDetected=False" in captured.err
    assert "wsCloseCode=1006" in captured.err


def test_send_terminal_command_no_diagnostics_on_success(capsys: pytest.CaptureFixture) -> None:
    """When WS returns {ok: True, diagnostics: {...}}, no diagnostics are logged."""
    diag = {
        "wsConnected": True,
        "promptDetected": True,
        "commandSent": True,
        "stdoutReceived": True,
        "stdoutLen": 42,
        "wsCloseCode": 1000,
        "wsCloseReason": "",
        "elapsed": 3000,
    }
    page = _make_eval_page({"ok": True, "errors": [], "diagnostics": diag})
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
    )
    assert result is True
    captured = capsys.readouterr()
    assert "ws-diagnostics" not in captured.err


def test_send_terminal_command_no_diagnostics_key_still_works() -> None:
    """Backward compat: {ok: False, errors: []} without diagnostics key works silently."""
    page = _make_eval_page({"ok": False, "errors": []})
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
    )
    assert result is False


# ---------------------------------------------------------------------------
# Phase 2: WS output listener — unit tests
# ---------------------------------------------------------------------------


def test_attach_ws_output_listener_returns_true() -> None:
    """_attach_ws_output_listener returns True when evaluate succeeds."""

    class _EvalFrame:
        def evaluate(self, script, payload=None):  # noqa: ANN001, ANN201
            return True

    result = _attach_ws_output_listener(
        _EvalFrame(),
        ws_url="wss://example.test/terminals/websocket/1",
        completion_marker="DONE",
        error_markers=["ERROR"],
    )
    assert result is True


def test_attach_ws_output_listener_returns_false_on_error() -> None:
    """_attach_ws_output_listener returns False when evaluate raises."""

    class _BrokenFrame:
        def evaluate(self, script, payload=None):  # noqa: ANN001, ANN201
            raise RuntimeError("evaluate failed")

    result = _attach_ws_output_listener(
        _BrokenFrame(),
        ws_url="wss://example.test/terminals/websocket/1",
        completion_marker="DONE",
        error_markers=["ERROR"],
    )
    assert result is False


def test_poll_ws_capture_returns_state() -> None:
    """_poll_ws_capture returns the capture dict from window._inspireWsCapture."""
    capture_state = {
        "done": True,
        "errors": ["SOME_ERROR"],
        "markerFound": False,
        "wsConnected": True,
        "stdoutReceived": True,
        "stdoutLen": 100,
        "wsCloseCode": 1000,
        "wsCloseReason": "",
        "elapsed": 5000,
    }

    class _EvalFrame:
        def evaluate(self, script):  # noqa: ANN001, ANN201
            return capture_state

    result = _poll_ws_capture(_EvalFrame())
    assert result == capture_state


def test_poll_ws_capture_returns_empty_on_error() -> None:
    """_poll_ws_capture returns safe default dict when evaluate raises."""

    class _BrokenFrame:
        def evaluate(self, script):  # noqa: ANN001, ANN201
            raise RuntimeError("evaluate failed")

    result = _poll_ws_capture(_BrokenFrame())
    assert result["done"] is False
    assert result["errors"] == []
    assert result["wsConnected"] is False


def test_detach_ws_output_listener_does_not_raise() -> None:
    """_detach_ws_output_listener does not raise even when evaluate fails."""

    class _BrokenFrame:
        def evaluate(self, script):  # noqa: ANN001, ANN201
            raise RuntimeError("evaluate failed")

    # Should not raise
    _detach_ws_output_listener(_BrokenFrame())


def test_wait_for_ws_capture_polls_until_done() -> None:
    """_wait_for_ws_capture polls and returns when done=True."""
    call_count = 0

    class _PollingFrame:
        def evaluate(self, script):  # noqa: ANN001, ANN201
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                return {
                    "done": True,
                    "errors": [],
                    "markerFound": True,
                    "wsConnected": True,
                    "stdoutReceived": True,
                    "stdoutLen": 50,
                    "wsCloseCode": None,
                    "wsCloseReason": "",
                    "elapsed": 2000,
                }
            return {
                "done": False,
                "errors": [],
                "markerFound": False,
                "wsConnected": True,
                "stdoutReceived": False,
                "stdoutLen": 0,
                "wsCloseCode": None,
                "wsCloseReason": "",
                "elapsed": 500,
            }

    class _DummyPage:
        def wait_for_timeout(self, ms: int) -> None:
            pass

    result = _wait_for_ws_capture(
        _PollingFrame(), _DummyPage(), timeout_ms=10000, poll_interval_ms=10
    )
    assert result["done"] is True
    assert result["markerFound"] is True
    assert call_count >= 3
