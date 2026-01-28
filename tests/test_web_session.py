import json
import pytest

from inspire.cli.utils import web_session as ws
from inspire.cli.utils.web_session import WebSession


class DummyResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
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


class DummyAPIResponse:
    def __init__(self, status: int = 200, payload=None) -> None:
        self.status = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class DummyRequestContext:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(("GET", url, headers, None, timeout))
        return DummyAPIResponse(200, {"ok": True})

    def post(self, url, headers=None, data=None, timeout=None):  # noqa: ANN001
        self.calls.append(("POST", url, headers, data, timeout))
        return DummyAPIResponse(200, {"ok": True})


class DummyBrowserContext:
    def __init__(self) -> None:
        self.request = DummyRequestContext()


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


def test_request_json_non_json_triggers_fallback(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(200, payload=ValueError("bad json")))
    browser = DummyBrowserClient({"ok": True})

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: browser)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    result = ws.request_json(session, "GET", "https://example.test")

    assert result == {"ok": True}
    assert ws._BROWSER_API_FORCE_BROWSER is True
    assert http.calls
    assert browser.calls


def test_browser_client_reset_on_expired(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    class ExpiringBrowserClient:
        def request_json(self, *_args, **_kwargs):
            raise ws.SessionExpiredError("expired")

    closed = {"called": False}

    def fake_close() -> None:
        closed["called"] = True

    def fake_get_web_session(**_kwargs):
        # Simulate re-authentication failure by raising SessionExpiredError
        raise ws.SessionExpiredError("re-auth failed")

    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: ExpiringBrowserClient())
    monkeypatch.setattr(ws, "_close_browser_client", fake_close)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", True)
    monkeypatch.setattr(ws, "get_web_session", fake_get_web_session)

    with pytest.raises(ws.SessionExpiredError):
        ws.request_json(session, "GET", "https://example.test")

    assert closed["called"] is True


def test_browser_request_context_posts_json_bytes():
    client = ws._BrowserRequestClient.__new__(ws._BrowserRequestClient)
    context = DummyBrowserContext()
    client._context = context
    client.session_fingerprint = "test"

    result = client.request_json("POST", "https://example.test", body={"a": 1})

    assert result == {"ok": True}
    assert context.request.calls
    method, _url, headers, data, _timeout = context.request.calls[0]
    assert method == "POST"
    assert json.loads(data) == {"a": 1}
    header_keys = {key.lower() for key in (headers or {})}
    assert "content-type" in header_keys
