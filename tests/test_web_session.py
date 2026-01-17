import pytest

from inspire.cli.utils import web_session as ws
from inspire.cli.utils.web_session import WebSession


class DummyResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class DummyHTTP:
    def __init__(self, response: DummyResponse) -> None:
        self.response = response
        self.calls = []

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(("GET", url, headers, timeout))
        return self.response

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001
        self.calls.append(("POST", url, headers, json, timeout))
        return self.response

    def close(self) -> None:
        pass


class DummyBrowserClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request_json(self, method, url, headers=None, body=None, timeout=30):  # noqa: ANN001
        self.calls.append((method, url, headers, body, timeout))
        return self.payload


def test_request_json_falls_back_to_browser_client(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(401))
    browser = DummyBrowserClient({"ok": True})

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: browser)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    result = ws.request_json(session, "GET", "https://example.test")

    assert result == {"ok": True}
    assert ws._BROWSER_API_FORCE_BROWSER is True
    assert http.calls
    assert browser.calls
