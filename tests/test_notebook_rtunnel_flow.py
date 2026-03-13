"""Tests for notebook rtunnel proxy verification flow helpers."""

from __future__ import annotations

import pytest

from inspire.platform.web.browser_api.rtunnel import flow as flow_module
from inspire.platform.web.browser_api.rtunnel.commands import SSHD_MISSING_MARKER


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

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        calls.append(proxy_url)
        if proxy_url == primary_url:
            raise AssertionError("primary probe should not be attempted when vscode passes")

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)

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

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        calls.append(proxy_url)
        if proxy_url == derived_url:
            raise ValueError("vscode failed\nLast response: 404 page not found")

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == primary_url
    assert calls == [derived_url, primary_url]
    assert len(diagnostics) == 1
    assert diagnostics[0].startswith("derived=")
    assert "Last response: 404 page not found" in diagnostics[0]


def test_ensure_proxy_readiness_does_not_raise_when_both_probes_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    derived_url = "https://nat.example/vscode/nb/proxy/31337/"
    fallback_url = "https://nat.example/vscode/nb/proxy/31337/?token=abc"
    calls: list[str] = []

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        calls.append(proxy_url)
        raise ValueError(f"probe failed for {proxy_url}\nLast response: 404 page not found")

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)
    monkeypatch.setattr(flow_module, "_build_vscode_proxy_url", lambda _page, port: fallback_url)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == fallback_url
    assert calls == [derived_url, primary_url, fallback_url]
    assert len(diagnostics) == 3
    assert diagnostics[0].startswith("derived=")
    assert diagnostics[1].startswith("primary=")
    assert diagnostics[2].startswith("fallback=")


def test_ensure_proxy_readiness_without_fallback_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    derived_url = "https://nat.example/vscode/nb/proxy/31337/"
    calls: list[str] = []

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        calls.append(proxy_url)
        raise ValueError(f"probe failed for {proxy_url}\nLast response: 404 page not found")

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)
    monkeypatch.setattr(flow_module, "_build_vscode_proxy_url", lambda _page, port: None)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    # When all probes fail and no page-built fallback is found, the primary
    # (jupyter) URL should be returned as the best guess -- not the
    # speculative derived vscode URL.
    assert resolved == primary_url
    assert calls == [derived_url, primary_url]
    assert len(diagnostics) == 2
    assert diagnostics[0].startswith("derived=")
    assert diagnostics[1].startswith("primary=")


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

    def fake_ws_send(*, context, lab_frame, batch_cmd, detected_errors=None):  # noqa: ANN202
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

    def fake_ws_send(*, context, lab_frame, batch_cmd, detected_errors=None):  # noqa: ANN202
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

    def fake_ws_send(*, context, lab_frame, batch_cmd, detected_errors=None):  # noqa: ANN202
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


def test_setup_raises_on_sshd_missing_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_setup_notebook_rtunnel_sync raises RuntimeError when sshd marker is detected."""
    _setup_sync_mocks(
        monkeypatch,
        setup_return=(True, [SSHD_MISSING_MARKER]),
    )

    with pytest.raises(RuntimeError, match="apt_mirror_url"):
        flow_module._setup_notebook_rtunnel_sync(notebook_id="test-nb")


def test_setup_raises_on_sshd_missing_marker_ws_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same but WS returned False (timeout) — the marker should still trigger the error."""
    _setup_sync_mocks(
        monkeypatch,
        setup_return=(False, [SSHD_MISSING_MARKER]),
    )

    with pytest.raises(RuntimeError, match="sshd_deb_dir"):
        flow_module._setup_notebook_rtunnel_sync(notebook_id="test-nb")
