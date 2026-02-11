"""Tests for notebook Jupyter URL helpers."""

from __future__ import annotations

from inspire.platform.web.browser_api import playwright_notebooks as notebooks_module
from inspire.platform.web.browser_api.playwright_notebooks import build_jupyter_proxy_url


def test_build_jupyter_proxy_url_includes_token_from_path() -> None:
    lab_url = (
        "https://nat-notebook-inspire.sii.edu.cn/ws-xxx/project-yyy/user-zzz/"
        "jupyter/notebook-123/token-abc/lab"
    )

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url.endswith("/proxy/31337/?token=token-abc")


def test_build_jupyter_proxy_url_prefers_query_token() -> None:
    lab_url = (
        "https://nat-notebook-inspire.sii.edu.cn/ws-xxx/project-yyy/user-zzz/"
        "jupyter/notebook-123/token-abc/lab?token=query-token"
    )

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url.endswith("/proxy/31337/?token=query-token")


def test_build_jupyter_proxy_url_notebook_lab_pattern() -> None:
    lab_url = "https://qz.sii.edu.cn/api/v1/notebook/lab/notebook-123/"

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url == "https://qz.sii.edu.cn/api/v1/notebook/lab/notebook-123/proxy/31337/"


class _FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakePage:
    def __init__(self, fake_time: list[float]) -> None:
        self._fake_time = fake_time
        self.goto_calls: list[str] = []
        self.wait_calls = 0
        self._frames: list[_FakeFrame] = []
        self.url = ""

    @property
    def frames(self) -> list[_FakeFrame]:
        return self._frames

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        assert timeout > 0
        assert wait_until == "domcontentloaded"
        self.goto_calls.append(url)
        self.url = url
        if "/ide?notebook_id=" in url:
            self._frames = []
        elif "/api/v1/notebook/lab/" in url:
            self._frames = [_FakeFrame(url)]

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls += 1
        self._fake_time[0] += timeout_ms / 1000.0


def test_open_notebook_lab_falls_back_early_to_direct_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake_time = [0.0]
    page = _FakePage(fake_time)
    monkeypatch.setattr(notebooks_module, "_get_base_url", lambda: "https://qz.sii.edu.cn")
    monkeypatch.setattr(notebooks_module, "_browser_api_path", lambda path: f"/api/v1{path}")
    monkeypatch.setattr(notebooks_module.time, "time", lambda: fake_time[0])

    lab = notebooks_module.open_notebook_lab(page, notebook_id="nb-123", timeout=60000)

    assert lab is not None
    assert len(page.goto_calls) == 2
    assert page.goto_calls[0] == "https://qz.sii.edu.cn/ide?notebook_id=nb-123"
    assert page.goto_calls[1] == "https://qz.sii.edu.cn/api/v1/notebook/lab/nb-123/"
    assert fake_time[0] < 20.0
