"""Tests for notebook rtunnel proxy verification flow helpers."""

from __future__ import annotations

import pytest

from inspire.platform.web.browser_api.rtunnel import flow as flow_module
from inspire.platform.web.browser_api.rtunnel.commands import (
    SSH_SERVER_MISSING_MARKER,
    SSHD_MISSING_MARKER,
)


class DummyLocator:
    def __init__(self, count: int = 0) -> None:
        self._count = count
        self.first = self

    def count(self) -> int:
        return self._count

    def click(self, timeout: int = 0) -> None:
        assert timeout >= 0


class DummyPage:
    def locator(self, selector: str) -> DummyLocator:
        assert selector
        return DummyLocator(count=0)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        assert timeout_ms >= 0


@pytest.mark.parametrize("timeout", [30, 120])
def test_ensure_proxy_readiness_prefers_vscode_when_available(
    monkeypatch: pytest.MonkeyPatch,
    timeout: int,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    derived_url = "https://nat.example/vscode/nb/proxy/31337/"
    calls: list[str] = []

    def fake_probe_rtunnel_proxy_once(*, proxy_url, context, request_timeout_ms):  # type: ignore[no-untyped-def]
        assert request_timeout_ms > 0
        assert context is not None
        calls.append(proxy_url)
        assert proxy_url == derived_url
        return True, "200 ok"

    monkeypatch.setattr(flow_module, "probe_rtunnel_proxy_once", fake_probe_rtunnel_proxy_once)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=timeout,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == derived_url
    assert calls == [derived_url]
    assert diagnostics == []


def test_ensure_proxy_readiness_falls_back_to_primary_when_vscode_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    derived_url = "https://nat.example/vscode/nb/proxy/31337/"
    calls: list[str] = []

    def fake_probe_rtunnel_proxy_once(*, proxy_url, context, request_timeout_ms):  # type: ignore[no-untyped-def]
        assert request_timeout_ms > 0
        assert context is not None
        calls.append(proxy_url)
        assert proxy_url == derived_url
        return False, "404 page not found"

    monkeypatch.setattr(flow_module, "probe_rtunnel_proxy_once", fake_probe_rtunnel_proxy_once)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == primary_url
    assert calls == [derived_url]
    assert len(diagnostics) == 1
    assert diagnostics[0].startswith("derived_vscode=")
    assert "404 page not found" in diagnostics[0]


def test_ensure_proxy_readiness_probes_primary_when_no_vscode_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    calls: list[str] = []

    def fake_probe_rtunnel_proxy_once(*, proxy_url, context, request_timeout_ms):  # type: ignore[no-untyped-def]
        assert request_timeout_ms > 0
        assert context is not None
        calls.append(proxy_url)
        assert proxy_url == primary_url
        return True, "200 ok"

    monkeypatch.setattr(flow_module, "probe_rtunnel_proxy_once", fake_probe_rtunnel_proxy_once)
    monkeypatch.setattr(flow_module, "_derive_vscode_proxy_url", lambda _url: None)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == primary_url
    assert calls == [primary_url]
    assert diagnostics == []


def test_ensure_proxy_readiness_returns_primary_after_failed_primary_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    calls: list[str] = []

    def fake_probe_rtunnel_proxy_once(*, proxy_url, context, request_timeout_ms):  # type: ignore[no-untyped-def]
        assert request_timeout_ms > 0
        assert context is not None
        calls.append(proxy_url)
        assert proxy_url == primary_url
        return False, "500 connect ECONNREFUSED 0.0.0.0:31337"

    monkeypatch.setattr(flow_module, "probe_rtunnel_proxy_once", fake_probe_rtunnel_proxy_once)
    monkeypatch.setattr(flow_module, "_derive_vscode_proxy_url", lambda _url: None)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == primary_url
    assert calls == [primary_url]
    assert diagnostics == ["primary=500 connect ECONNREFUSED 0.0.0.0:31337"]


# ---------------------------------------------------------------------------
# _send_rtunnel_setup_script — error propagation
# ---------------------------------------------------------------------------


class _DummyTimer:
    def mark(self, label: str) -> float:
        return 0.0

    def summary(self) -> None:
        pass


def test_send_rtunnel_setup_script_propagates_errors_on_ws_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS returns True + populates errors → returns (True, [marker])."""

    def fake_ws_send(
        *, context, lab_frame, batch_cmd, detected_errors=None, diagnostics_out=None
    ):  # noqa: ANN202
        if detected_errors is not None:
            detected_errors.append(SSHD_MISSING_MARKER)
        return True

    monkeypatch.setattr(flow_module, "_send_setup_command_via_terminal_ws", fake_ws_send)

    ok, errors = flow_module._send_rtunnel_setup_script(
        context=object(),
        page=DummyPage(),
        lab_frame=object(),
        batch_cmd="echo",
        timer=_DummyTimer(),
    )
    assert ok is True
    assert errors == [SSHD_MISSING_MARKER]


def test_send_rtunnel_setup_script_propagates_errors_on_ws_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS returns False + populates errors → returns (False, [marker]),
    NOT (False, []) from the browser fallback."""

    def fake_ws_send(
        *, context, lab_frame, batch_cmd, detected_errors=None, diagnostics_out=None
    ):  # noqa: ANN202
        if detected_errors is not None:
            detected_errors.append(SSHD_MISSING_MARKER)
        return False

    monkeypatch.setattr(flow_module, "_send_setup_command_via_terminal_ws", fake_ws_send)

    ok, errors = flow_module._send_rtunnel_setup_script(
        context=object(),
        page=DummyPage(),
        lab_frame=object(),
        batch_cmd="echo",
        timer=_DummyTimer(),
    )
    assert ok is False
    assert errors == [SSHD_MISSING_MARKER]


def test_send_rtunnel_setup_script_returns_empty_errors_on_clean_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS returns True with no errors → returns (True, [])."""

    def fake_ws_send(
        *, context, lab_frame, batch_cmd, detected_errors=None, diagnostics_out=None
    ):  # noqa: ANN202
        return True

    monkeypatch.setattr(flow_module, "_send_setup_command_via_terminal_ws", fake_ws_send)

    ok, errors = flow_module._send_rtunnel_setup_script(
        context=object(),
        page=DummyPage(),
        lab_frame=object(),
        batch_cmd="echo",
        timer=_DummyTimer(),
    )
    assert ok is True
    assert errors == []


def test_send_rtunnel_setup_script_skips_browser_replay_when_ws_command_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_ws_send(
        *,
        context,
        lab_frame,
        batch_cmd,
        detected_errors=None,
        diagnostics_out=None,
    ):  # noqa: ANN202
        if diagnostics_out is not None:
            diagnostics_out.update(
                {
                    "wsConnected": True,
                    "promptDetected": True,
                    "commandSent": True,
                    "stdoutReceived": True,
                    "stdoutLen": 2048,
                    "elapsed": 120000,
                }
            )
        return False

    monkeypatch.setattr(flow_module, "_send_setup_command_via_terminal_ws", fake_ws_send)
    monkeypatch.setattr(
        flow_module,
        "_open_or_create_terminal",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("browser fallback should not run")
        ),
    )

    ok, errors = flow_module._send_rtunnel_setup_script(
        context=object(),
        page=DummyPage(),
        lab_frame=object(),
        batch_cmd="echo",
        timer=_DummyTimer(),
    )
    assert ok is False
    assert errors == []


# ---------------------------------------------------------------------------
# _setup_notebook_rtunnel_sync — sshd marker → RuntimeError
# ---------------------------------------------------------------------------


class _FakeLocatorInner:
    def wait_for(self, **kwargs):  # noqa: ANN003, ANN202
        pass


class _FakeLocator:
    first = _FakeLocatorInner()


class _FakeFrame:
    url = "https://nb.example.com/lab"

    def locator(self, _sel: str) -> _FakeLocator:
        return _FakeLocator()


class _FakePage:
    frames: list = []

    def wait_for_timeout(self, _ms: int) -> None:
        pass


class _FakeContext:
    def new_page(self) -> _FakePage:
        return _FakePage()


class _FakeBrowser:
    pass


class _FakePlaywright:
    def __enter__(self) -> "_FakePlaywright":
        return self

    def __exit__(self, *_a: object) -> None:
        pass


class _FakeSession:
    login_username = "testuser"
    storage_state = {}


def _setup_sync_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    setup_return: tuple[bool, list[str]],
) -> None:
    """Wire all mocks needed to reach _send_rtunnel_setup_script inside
    _setup_notebook_rtunnel_sync."""
    import playwright.sync_api as pw_mod

    import inspire.platform.web.browser_api.playwright_notebooks as pn_mod

    monkeypatch.setattr(pw_mod, "sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(pn_mod, "open_notebook_lab", lambda page, **kw: _FakeFrame())
    monkeypatch.setattr(pn_mod, "build_jupyter_proxy_url", lambda url, **kw: "https://proxy/url")

    monkeypatch.setattr(flow_module, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(flow_module, "probe_existing_rtunnel_proxy_url", lambda **kw: None)
    monkeypatch.setattr(flow_module, "_timing_enabled", lambda: False)
    monkeypatch.setattr(flow_module, "_launch_browser", lambda p, headless: _FakeBrowser())
    monkeypatch.setattr(flow_module, "_new_context", lambda browser, storage_state: _FakeContext())
    monkeypatch.setattr(flow_module, "_resolve_rtunnel_binary", lambda **kw: None)
    monkeypatch.setattr(flow_module, "build_rtunnel_setup_commands", lambda **kw: ["echo test"])
    monkeypatch.setattr(flow_module, "_build_batch_setup_script", lambda _lines: "echo test")
    monkeypatch.setattr(flow_module, "_send_rtunnel_setup_script", lambda **kw: setup_return)
    monkeypatch.setattr(flow_module, "collect_notebook_rtunnel_diagnostics", lambda **kw: None)


def test_setup_raises_on_sshd_missing_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_setup_notebook_rtunnel_sync raises RuntimeError when sshd marker is detected."""
    _setup_sync_mocks(
        monkeypatch,
        setup_return=(True, [SSHD_MISSING_MARKER]),
    )

    with pytest.raises(RuntimeError, match="apt_mirror_url") as exc:
        flow_module._setup_notebook_rtunnel_sync(notebook_id="test-nb")
    assert "dropbear" in str(exc.value)


def test_setup_raises_on_sshd_missing_marker_ws_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same but WS returned False (timeout) — the marker should still trigger the error."""
    _setup_sync_mocks(
        monkeypatch,
        setup_return=(False, [SSHD_MISSING_MARKER]),
    )

    with pytest.raises(RuntimeError, match="no SSH server was installed"):
        flow_module._setup_notebook_rtunnel_sync(notebook_id="test-nb")


def test_setup_raises_on_generic_ssh_server_missing_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_sync_mocks(
        monkeypatch,
        setup_return=(True, [SSH_SERVER_MISSING_MARKER]),
    )

    with pytest.raises(RuntimeError, match="no SSH server process is running") as exc:
        flow_module._setup_notebook_rtunnel_sync(notebook_id="test-nb")
    assert "RTunnel trace summary:" in str(exc.value)


# ---------------------------------------------------------------------------
# Phase 2: Hybrid browser fallback with WS output capture
# ---------------------------------------------------------------------------


class _HybridFrame:
    url = "https://nb.example.com/lab"

    def locator(self, _sel: str):  # noqa: ANN201
        return _FakeLocator()


class _HybridKeyboard:
    def __init__(self) -> None:
        self.inserted: list[str] = []
        self.pressed: list[str] = []

    def insert_text(self, text: str) -> None:
        self.inserted.append(text)

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _HybridPage:
    def __init__(self) -> None:
        self.keyboard = _HybridKeyboard()

    def wait_for_timeout(self, _ms: int) -> None:
        pass


def _setup_hybrid_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ws_send_returns: bool = False,
    term_name: str | None = "term-1",
    attach_returns: bool = True,
    ws_capture_state: dict | None = None,
) -> dict:
    """Wire mocks for testing the hybrid browser fallback path."""
    track: dict = {
        "ws_send_called": False,
        "open_terminal_called": False,
        "focus_called": False,
        "attach_called": False,
        "detach_called": False,
        "delete_called": False,
    }

    def fake_ws_send(
        *, context, lab_frame, batch_cmd, detected_errors=None, diagnostics_out=None
    ):  # noqa: ANN202
        track["ws_send_called"] = True
        return ws_send_returns

    def fake_open_terminal(_ctx, _page, _frame):  # noqa: ANN202
        track["open_terminal_called"] = True
        return True, term_name

    def fake_focus(_frame, _page):  # noqa: ANN202
        track["focus_called"] = True
        return True

    def fake_attach(_frame, *, ws_url, completion_marker, error_markers):  # noqa: ANN202
        track["attach_called"] = True
        return attach_returns

    def fake_wait_capture(_frame, _page, *, timeout_ms, poll_interval_ms=500):  # noqa: ANN202
        return ws_capture_state or {"done": False, "errors": [], "markerFound": False}

    def fake_detach(_frame):  # noqa: ANN202
        track["detach_called"] = True

    def fake_build_ws_url(_url, _name):  # noqa: ANN202
        return "wss://nb.example.com/terminals/websocket/term-1"

    def fake_wait_surface(_frame, *, timeout_ms):  # noqa: ANN202
        return True

    def fake_delete(_ctx, *, lab_url, term_name):  # noqa: ANN202
        track["delete_called"] = True
        return True

    monkeypatch.setattr(flow_module, "_send_setup_command_via_terminal_ws", fake_ws_send)
    monkeypatch.setattr(flow_module, "_open_or_create_terminal", fake_open_terminal)
    monkeypatch.setattr(flow_module, "_focus_terminal_input", fake_focus)
    monkeypatch.setattr(flow_module, "_attach_ws_output_listener", fake_attach)
    monkeypatch.setattr(flow_module, "_wait_for_ws_capture", fake_wait_capture)
    monkeypatch.setattr(flow_module, "_detach_ws_output_listener", fake_detach)
    monkeypatch.setattr(flow_module, "_build_terminal_websocket_url", fake_build_ws_url)
    monkeypatch.setattr(flow_module, "_wait_for_terminal_surface", fake_wait_surface)
    monkeypatch.setattr(flow_module, "_delete_terminal_via_api", fake_delete)
    return track


def test_send_rtunnel_setup_script_hybrid_detects_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser fallback + WS listener captures SSHD_MISSING_MARKER → (True, [marker])."""
    track = _setup_hybrid_mocks(
        monkeypatch,
        ws_send_returns=False,
        ws_capture_state={
            "done": True,
            "errors": [SSHD_MISSING_MARKER],
            "markerFound": False,
        },
    )

    ok, errors = flow_module._send_rtunnel_setup_script(
        context=object(),
        page=_HybridPage(),
        lab_frame=_HybridFrame(),
        batch_cmd="echo test",
        timer=_DummyTimer(),
    )
    assert ok is True
    assert errors == [SSHD_MISSING_MARKER]
    assert track["attach_called"]
    assert track["detach_called"]


def test_send_rtunnel_setup_script_hybrid_detects_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS listener finds completion marker → (True, [])."""
    track = _setup_hybrid_mocks(
        monkeypatch,
        ws_send_returns=False,
        ws_capture_state={
            "done": True,
            "errors": [],
            "markerFound": True,
        },
    )

    ok, errors = flow_module._send_rtunnel_setup_script(
        context=object(),
        page=_HybridPage(),
        lab_frame=_HybridFrame(),
        batch_cmd="echo test",
        timer=_DummyTimer(),
    )
    assert ok is True
    assert errors == []
    assert track["attach_called"]
    assert track["detach_called"]


def test_send_rtunnel_setup_script_hybrid_skips_ws_without_term_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No term_name → no WS listener, returns (False, [])."""
    track = _setup_hybrid_mocks(
        monkeypatch,
        ws_send_returns=False,
        term_name=None,
    )

    ok, errors = flow_module._send_rtunnel_setup_script(
        context=object(),
        page=_HybridPage(),
        lab_frame=_HybridFrame(),
        batch_cmd="echo test",
        timer=_DummyTimer(),
    )
    assert ok is False
    assert errors == []
    assert not track["attach_called"]
    assert not track["detach_called"]


def test_send_rtunnel_setup_script_hybrid_cleans_up_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detach_ws_output_listener is called in finally block on exception."""
    track = _setup_hybrid_mocks(
        monkeypatch,
        ws_send_returns=False,
    )

    # Make keyboard.insert_text raise to simulate a failure after attach
    class _FailingPage:
        def __init__(self) -> None:
            self.keyboard = self

        def insert_text(self, text: str) -> None:
            raise RuntimeError("keyboard failed")

        def press(self, key: str) -> None:
            pass

        def wait_for_timeout(self, _ms: int) -> None:
            pass

    with pytest.raises(RuntimeError, match="keyboard failed"):
        flow_module._send_rtunnel_setup_script(
            context=object(),
            page=_FailingPage(),
            lab_frame=_HybridFrame(),
            batch_cmd="echo test",
            timer=_DummyTimer(),
        )
    assert track["attach_called"]
    assert track["detach_called"]
    assert track["delete_called"]
