import json
import logging
from pathlib import Path
from types import SimpleNamespace
import pytest
import requests

from inspire.config import Config
from inspire.platform.web.browser_api import workspaces as workspace_api
from inspire.platform.web import session as ws
from inspire.platform.web.session import auth as ws_auth
from inspire.platform.web.session import http_auth as http_auth_module
from inspire.platform.web.session import WebSession
from inspire.platform.web.session.http_auth import (
    _build_web_session,
    _classify_cas_login_error,
    _collect_qz_cookies,
    _extract_workspaces,
    _requests_cookie_to_playwright_format,
)


class DummyResponse:
    def __init__(
        self,
        status_code: int,
        payload=None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class DummyHTTP:
    def __init__(self, response: DummyResponse | list[DummyResponse]) -> None:
        responses = response if isinstance(response, list) else [response]
        self.responses = iter(responses)
        self.calls = []

    def _next_response(self) -> DummyResponse:
        return next(self.responses)

    def get(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001
        self.calls.append(("GET", url, headers, json, timeout))
        return self._next_response()

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001
        self.calls.append(("POST", url, headers, json, timeout))
        return self._next_response()

    def put(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001
        self.calls.append(("PUT", url, headers, json, timeout))
        return self._next_response()

    def delete(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001
        self.calls.append(("DELETE", url, headers, json, timeout))
        return self._next_response()

    def close(self) -> None:
        pass


class RefreshHTTP:
    def __init__(self, response: DummyResponse, cookies: dict[str, str]) -> None:
        self.response = response
        self.cookies = requests.cookies.RequestsCookieJar()
        for name, value in cookies.items():
            self.cookies.set(name, value, domain="example.test", path="/")
        self.get_calls: list[tuple[str, dict, dict[str, str]]] = []
        self.closed = False

    def get(self, url, **kwargs):  # noqa: ANN001
        self.get_calls.append((url, kwargs, dict(self.cookies)))
        self.cookies.set("inspire-session", "new", domain="example.test", path="/")
        return self.response

    def close(self) -> None:
        self.closed = True


def test_refresh_with_requests_reissues_qz_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    session = WebSession(
        storage_state={
            "cookies": [
                {
                    "name": "inspire-session",
                    "value": "old",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
            "origins": [{"origin": "https://example.test", "localStorage": []}],
        },
        cookies={"inspire-session": "old"},
        workspace_id="ws-test",
        account_alias="lab",
        login_username="user",
        base_url="https://example.test",
        created_at=1.0,
    )
    response = DummyResponse(200, {})
    response.url = "https://example.test/"
    http = RefreshHTTP(response, {"inspire-session": "old", "cas": "keep"})
    monkeypatch.setattr(http_auth_module, "build_requests_session", lambda *_a, **_k: http)
    monkeypatch.setattr(http_auth_module, "_v2_action", lambda *_a, **_k: {})

    refreshed = http_auth_module.refresh_with_requests(session)

    assert http.get_calls[0][0] == "https://example.test"
    assert http.get_calls[0][2].get("inspire-session") is None
    assert refreshed.cookies == {"inspire-session": "new", "cas": "keep"}
    assert refreshed.workspace_id == "ws-test"
    assert refreshed.account_alias == "lab"
    assert refreshed.storage_state["origins"] == session.storage_state["origins"]
    assert http.closed is True


def test_refresh_with_requests_preserves_host_cookie_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WebSession(
        storage_state={
            "cookies": [
                {
                    "name": "__Host-cas_state",
                    "value": "old",
                    "domain": "example.test",
                    "path": "/",
                    "secure": False,
                    "httpOnly": True,
                },
                {
                    "name": "inspire-session",
                    "value": "old",
                    "domain": "example.test",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
            ],
            "origins": [],
        },
        cookies={"__Host-cas_state": "old", "inspire-session": "old"},
        base_url="https://example.test",
        created_at=1.0,
    )
    response = DummyResponse(200, {})
    response.url = "https://example.test/"
    http = RefreshHTTP(response, {"__Host-cas_state": "old", "inspire-session": "old"})
    monkeypatch.setattr(http_auth_module, "build_requests_session", lambda *_a, **_k: http)
    monkeypatch.setattr(http_auth_module, "_v2_action", lambda *_a, **_k: {})

    refreshed = http_auth_module.refresh_with_requests(session)

    by_name = {cookie["name"]: cookie for cookie in refreshed.storage_state["cookies"]}
    assert by_name["__Host-cas_state"]["secure"] is True
    assert by_name["__Host-cas_state"]["httpOnly"] is True
    assert by_name["__Host-cas_state"]["path"] == "/"
    assert by_name["inspire-session"]["sameSite"] == "Lax"


def test_refresh_with_requests_rejects_an_expired_sso_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WebSession(
        storage_state={
            "cookies": [
                {
                    "name": "inspire-session",
                    "value": "old",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
            "origins": [],
        },
        cookies={"inspire-session": "old"},
        base_url="https://example.test",
        created_at=1.0,
    )
    response = DummyResponse(200, {})
    response.url = "https://cas.example.test/login"
    http = RefreshHTTP(response, {"inspire-session": "old"})
    monkeypatch.setattr(http_auth_module, "build_requests_session", lambda *_a, **_k: http)

    with pytest.raises(http_auth_module.SessionAuthenticationError):
        http_auth_module.refresh_with_requests(session)
    assert http.closed is True


def test_request_json_http_only(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(200, {"ok": True}))

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)

    result = ws.request_json(session, "GET", "https://example.test")

    assert result == {"ok": True}
    assert http.calls == [("GET", "https://example.test", {}, None, 30)]


@pytest.mark.parametrize("body", [{"state": "running"}, ["job-1", "job-2"]])
def test_request_json_get_sends_json_body(
    monkeypatch: pytest.MonkeyPatch, body: dict | list
) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    http = DummyHTTP(DummyResponse(200, {"ok": True}))
    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)

    assert ws.request_json(session, "GET", "https://example.test", body=body) == {"ok": True}
    assert http.calls == [
        (
            "GET",
            "https://example.test",
            {"Content-Type": "application/json"},
            body,
            30,
        )
    ]


def test_request_json_supports_put_json_body(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(200, {"ok": True}))

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)

    result = ws.request_json(session, "PUT", "https://example.test", body={"name": "new"})

    assert result == {"ok": True}
    assert http.calls == [
        (
            "PUT",
            "https://example.test",
            {"Content-Type": "application/json"},
            {"name": "new"},
            30,
        )
    ]


def test_request_json_preserves_an_empty_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    http = DummyHTTP(DummyResponse(200, {"ok": True}))
    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)

    assert ws.request_json(session, "POST", "https://example.test", body=[]) == {"ok": True}
    assert http.calls == [
        (
            "POST",
            "https://example.test",
            {"Content-Type": "application/json"},
            [],
            30,
        )
    ]


def test_request_json_retries_429_using_apisix_reset_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    http = DummyHTTP(
        [
            DummyResponse(429, text="rate limited", headers={"X-RateLimit-Reset": "1"}),
            DummyResponse(200, {"ok": True}),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws.time, "sleep", sleeps.append)

    assert ws.request_json(session, "GET", "https://example.test/api/v1/groups") == {"ok": True}
    assert len(http.calls) == 2
    assert sleeps == [1.05]


def test_request_json_bounds_429_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    http = DummyHTTP([DummyResponse(429, text="rate limited") for _ in range(4)])
    sleeps: list[float] = []
    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws.time, "sleep", sleeps.append)

    with pytest.raises(ValueError, match="API returned 429"):
        ws.request_json(session, "GET", "https://example.test/api/v1/groups")

    assert len(http.calls) == 4
    assert sleeps == [0.5, 1.0, 2.0]


def test_request_json_supports_a_longer_per_request_429_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    http = DummyHTTP(
        [
            DummyResponse(429, text="rate limited", headers={"Retry-After": "0.1"}),
            DummyResponse(429, text="rate limited", headers={"Retry-After": "0.1"}),
            DummyResponse(429, text="rate limited", headers={"Retry-After": "0.1"}),
            DummyResponse(200, {"ok": True}),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws.time, "sleep", sleeps.append)

    result = ws.request_json(
        session,
        "POST",
        "https://example.test/api/v1/train_job/create",
        rate_limit_max_retries=7,
        rate_limit_default_delay_seconds=1.0,
        rate_limit_max_delay_seconds=30.0,
    )

    assert result == {"ok": True}
    assert len(http.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_request_json_raises_typed_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    http = DummyHTTP([DummyResponse(429, text="slow down") for _ in range(3)])
    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws.time, "sleep", lambda _delay: None)

    with pytest.raises(ws.RateLimitError) as excinfo:
        ws.request_json(
            session,
            "POST",
            "https://example.test/api/v1/train_job/create",
            rate_limit_max_retries=2,
        )

    assert excinfo.value.path == "/api/v1/train_job/create"
    assert excinfo.value.attempts == 3


def test_request_json_non_json_raises_session_expired(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(200, payload=ValueError("bad json")))

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "clear_session_cache", lambda: None)
    monkeypatch.setattr(
        ws,
        "refresh_with_requests",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("SSO cookies expired")),
    )
    monkeypatch.setattr(
        ws,
        "get_web_session",
        lambda **_: (_ for _ in ()).throw(ws.SessionExpiredError("re-auth failed")),
    )

    with pytest.raises(ws.SessionExpiredError):
        ws.request_json(session, "GET", "https://example.test")


def test_request_json_refreshes_sso_cookies_before_reauthentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        cookies={"session": "old"},
        workspace_id="ws-old",
        account_alias="lab",
        login_username="old-user",
        base_url="https://example.test",
        created_at=1.0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-new",
        account_alias="lab",
        login_username="new-user",
        created_at=2.0,
    )
    calls = {"sso": 0, "reauth": 0, "saved": 0}

    def fake_refresh(current, base_url):  # noqa: ANN001
        assert current is session
        assert base_url == "https://example.test"
        calls["sso"] += 1
        return refreshed

    def fake_reauth(**_kwargs):
        calls["reauth"] += 1
        raise AssertionError("full re-authentication should not run")

    def fake_save(**_kwargs):
        calls["saved"] += 1

    monkeypatch.setattr(ws, "refresh_with_requests", fake_refresh)
    monkeypatch.setattr(ws, "get_web_session", fake_reauth)
    monkeypatch.setattr(session, "save", fake_save)

    http_first = DummyHTTP(DummyResponse(401))
    http_second = DummyHTTP(DummyResponse(200, {"ok": True}))
    call_count = {"n": 0}

    def fake_build_requests_session(_session, _url):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return http_first
        return http_second

    monkeypatch.setattr(ws, "build_requests_session", fake_build_requests_session)

    assert ws.request_json(session, "GET", "https://example.test/api/v2/user") == {"ok": True}
    assert calls == {"sso": 1, "reauth": 0, "saved": 1}
    assert session.storage_state == refreshed.storage_state
    assert session.cookies == refreshed.cookies
    assert session.workspace_id == refreshed.workspace_id
    assert session.login_username == refreshed.login_username
    assert session.created_at == refreshed.created_at
    assert session._refresh_attempted is False


def test_request_json_reauth_refreshes_session_in_place(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.CaptureFixture[str]
):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        cookies={"session": "old"},
        workspace_id="ws-old",
        login_username="old-user",
        created_at=1.0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-new",
        login_username="new-user",
        created_at=2.0,
    )

    refresh_calls = {"count": 0}
    sso_calls = {"count": 0}

    def fake_get_web_session(**_kwargs):
        refresh_calls["count"] += 1
        return refreshed

    def fail_sso(*_args, **_kwargs):
        sso_calls["count"] += 1
        raise RuntimeError("SSO cookies expired")

    monkeypatch.setattr(ws, "clear_session_cache", lambda: None)
    monkeypatch.setattr(ws, "refresh_with_requests", fail_sso)
    monkeypatch.setattr(ws, "get_web_session", fake_get_web_session)

    http_first = DummyHTTP(DummyResponse(401))
    http_second = DummyHTTP(DummyResponse(200, {"ok": True}))
    call_count = {"n": 0}

    def fake_build_requests_session(_session, _url):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return http_first
        return http_second

    monkeypatch.setattr(ws, "build_requests_session", fake_build_requests_session)

    with caplog.at_level(logging.WARNING, logger="inspire.platform.web.session"):
        result = ws.request_json(session, "GET", "https://example.test")
    assert result == {"ok": True}
    assert refresh_calls["count"] == 1
    assert sso_calls["count"] == 1
    assert session.storage_state == refreshed.storage_state
    assert session.cookies == refreshed.cookies
    assert session.workspace_id == refreshed.workspace_id
    assert session.login_username == refreshed.login_username
    assert session.created_at == refreshed.created_at
    assert "Session expired, re-authenticating..." in caplog.text


def test_request_json_allows_a_later_independent_refresh(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        cookies={"session": "old"},
        workspace_id="ws-old",
        account_alias="lab",
        created_at=1.0,
    )
    refresh_calls = 0
    sso_calls = 0
    response_codes = iter((401, 200, 401, 200))

    def fail_sso(*_args, **_kwargs):
        nonlocal sso_calls
        sso_calls += 1
        raise RuntimeError("SSO cookies expired")

    def refresh(**_kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        return WebSession(
            storage_state={"cookies": [{"name": "session", "value": f"new-{refresh_calls}"}]},
            cookies={"session": f"new-{refresh_calls}"},
            workspace_id="ws-new",
            account_alias="lab",
            created_at=float(refresh_calls + 1),
        )

    monkeypatch.setattr(ws, "refresh_with_requests", fail_sso)
    monkeypatch.setattr(ws, "get_web_session", refresh)
    monkeypatch.setattr(
        ws,
        "build_requests_session",
        lambda _session, _url: DummyHTTP(
            DummyResponse(next(response_codes), {"request": refresh_calls})
        ),
    )

    assert ws.request_json(session, "GET", "https://example.test") == {"request": 1}
    assert ws.request_json(session, "GET", "https://example.test") == {"request": 2}
    assert refresh_calls == 2
    assert sso_calls == 2
    assert session._refresh_attempted is False
    assert session._refresh_failure_message is None


def test_request_json_latches_refresh_failure_for_the_session(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.CaptureFixture[str]
):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        cookies={"session": "old"},
        workspace_id="ws-old",
        account_alias="lab",
        created_at=1.0,
    )
    refresh_calls = 0
    http_calls = 0
    sso_calls = 0

    def fail_sso(*_args, **_kwargs):
        nonlocal sso_calls
        sso_calls += 1
        raise RuntimeError("SSO cookies expired")

    def fail_refresh(**_kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        raise RuntimeError("Wrong username or password")

    def build_http(_session, _url):
        nonlocal http_calls
        http_calls += 1
        return DummyHTTP(DummyResponse(401))

    monkeypatch.setattr(ws, "refresh_with_requests", fail_sso)
    monkeypatch.setattr(ws, "get_web_session", fail_refresh)
    monkeypatch.setattr(ws, "build_requests_session", build_http)

    with caplog.at_level(logging.WARNING, logger="inspire.platform.web.session"):
        for _ in range(2):
            with pytest.raises(ws.SessionRefreshError, match="Wrong username or password"):
                ws.request_json(session, "GET", "https://example.test")

    assert refresh_calls == 1
    assert http_calls == 1
    assert sso_calls == 1
    assert caplog.text.count("Session expired, re-authenticating...") == 1


def test_workspace_enumeration_propagates_session_refresh_failure(monkeypatch) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        workspace_id="ws-11111111-1111-1111-1111-111111111111",
        created_at=1.0,
    )
    monkeypatch.setattr(
        workspace_api,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ws.SessionRefreshError("Session re-authentication failed")
        ),
    )

    with pytest.raises(ws.SessionRefreshError, match="re-authentication failed"):
        workspace_api.try_enumerate_workspaces(session)


def test_workspace_enumeration_strict_mode_preserves_lookup_failure(monkeypatch) -> None:
    session = WebSession(
        storage_state={"cookies": []},
        workspace_id="ws-11111111-1111-1111-1111-111111111111",
        created_at=1.0,
    )
    monkeypatch.setattr(
        workspace_api,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("workspace list unavailable")),
    )

    assert workspace_api.try_enumerate_workspaces(session) == []
    with pytest.raises(RuntimeError, match="workspace list unavailable"):
        workspace_api.try_enumerate_workspaces(session, strict=True)


def test_workspace_enumeration_preserves_platform_metadata(monkeypatch) -> None:
    workspace_id = "ws-11111111-1111-1111-1111-111111111111"
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "ok"}]},
        workspace_id=workspace_id,
        created_at=1.0,
    )
    monkeypatch.setattr(
        workspace_api,
        "_request_json",
        lambda *_args, **_kwargs: {
            "code": 0,
            "data": {
                "items": [
                    {
                        "id": workspace_id,
                        "name": "Fair GPU",
                        "is_fair_workspace": True,
                        "type": "PUBLIC",
                        "compute_groups": [{"compute_group_id": "lcg-1", "name": "GPU"}],
                    }
                ]
            },
        },
    )

    assert workspace_api.try_enumerate_workspaces(session) == [
        {
            "id": workspace_id,
            "name": "Fair GPU",
            "is_fair_workspace": True,
            "type": "PUBLIC",
            "compute_groups": [{"compute_group_id": "lcg-1", "name": "GPU"}],
        }
    ]


def test_get_credentials_prefers_project_toml_when_prefer_source_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    global_dir = tmp_path / ".config" / "inspire"
    global_dir.mkdir(parents=True)
    (global_dir / "config.toml").write_text(
        """
[accounts."toml-user"]
password = "global-pass"
"""
    )

    project_dir = tmp_path / ".inspire"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text(
        """
[cli]
prefer_source = "toml"

[auth]
username = "toml-user"
"""
    )
    monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_dir / "config.toml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INSPIRE_USERNAME", "env-user")
    monkeypatch.setenv("INSPIRE_PASSWORD", "env-pass")

    username, password = ws.get_credentials()

    assert username == "toml-user"
    assert password == "global-pass"


def test_get_web_session_reauths_when_cached_user_mismatch(monkeypatch: pytest.MonkeyPatch):
    cached = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        login_username="old-user",
        created_at=0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-test",
        login_username="new-user",
        created_at=1,
    )
    calls: dict[str, str] = {}
    config = Config(
        username="new-user",
        password="new-pass",
        base_url="https://example.invalid",
    )

    monkeypatch.setattr(
        ws_auth.WebSession,
        "load",
        classmethod(lambda cls, allow_expired=False, account=None: cached),
    )
    monkeypatch.setattr(ws_auth, "_load_runtime_config", lambda account_alias=None: config)
    monkeypatch.setattr(ws_auth.WebSession, "save", lambda self, account=None: None)

    def fake_login(username: str, password: str, base_url: str = "", headless: bool = True):
        calls["username"] = username
        calls["password"] = password
        calls["base_url"] = base_url
        calls["headless"] = str(headless)
        return refreshed

    monkeypatch.setattr(ws_auth, "login_with_requests", fake_login)
    monkeypatch.setattr(ws_auth, "_get_auth_method", lambda config=None: "http")

    session = ws_auth.get_web_session(force_refresh=False, require_workspace=False)

    assert session is refreshed
    assert calls["username"] == "new-user"
    assert calls["password"] == "new-pass"
    assert calls["base_url"] == "https://example.invalid"


def test_get_web_session_uses_alias_scoped_cache(monkeypatch: pytest.MonkeyPatch):
    cached = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        account_alias="project",
        login_username="project-user",
        base_url="https://example.invalid",
        created_at=0,
    )
    config = Config(
        username="project-user",
        password="secret",
        account_alias="project",
        base_url="https://example.invalid",
    )
    load_calls: list[str | None] = []

    def fake_load(cls, allow_expired=False, account=None):  # type: ignore[no-untyped-def]
        load_calls.append(account)
        return cached

    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(fake_load))
    monkeypatch.setattr(ws_auth, "_load_runtime_config", lambda account_alias=None: config)

    session = ws_auth.get_web_session(force_refresh=False, require_workspace=False)

    assert session is cached
    assert load_calls
    assert load_calls[0] == "project"


def test_get_web_session_rejects_cache_from_other_alias_with_same_username(
    monkeypatch: pytest.MonkeyPatch,
):
    cached = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        account_alias="personal",
        login_username="shared-user",
        base_url="https://example.invalid",
        created_at=0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-test",
        login_username="shared-user",
        created_at=1,
    )
    config = Config(
        username="shared-user",
        password="lab-pass",
        account_alias="lab",
        base_url="https://example.invalid",
    )
    login_calls: list[str] = []

    monkeypatch.setattr(
        ws_auth.WebSession,
        "load",
        classmethod(lambda cls, allow_expired=False, account=None: cached),
    )
    monkeypatch.setattr(ws_auth, "_load_runtime_config", lambda account_alias=None: config)
    monkeypatch.setattr(ws_auth, "_get_auth_method", lambda config=None: "http")
    monkeypatch.setattr(ws_auth.WebSession, "save", lambda self, account=None: None)
    monkeypatch.setattr(
        ws_auth,
        "login_with_requests",
        lambda username, password, base_url="", headless=True: (
            login_calls.append(username) or refreshed
        ),
    )

    session = ws_auth.get_web_session(account="lab")

    assert session is refreshed
    assert session.account_alias == "lab"
    assert login_calls == ["shared-user"]


def test_get_web_session_force_refresh_bypasses_cache(monkeypatch: pytest.MonkeyPatch):
    cached = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        cookies={"session": "old"},
        workspace_id="ws-old",
        login_username="refresh-user",
        created_at=0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-new",
        login_username="refresh-user",
        created_at=1,
    )
    load_calls: list[tuple[bool, str | None]] = []
    login_calls: dict[str, str] = {}
    save_calls: list[str | None] = []
    config = Config(
        username="refresh-user",
        password="refresh-pass",
        account_alias="lab",
        base_url="https://example.invalid",
    )

    def fake_load(cls, allow_expired=False, account=None):  # type: ignore[no-untyped-def]
        load_calls.append((allow_expired, account))
        return cached

    def fake_login(username: str, password: str, base_url: str = "", headless: bool = True):
        login_calls["username"] = username
        login_calls["password"] = password
        login_calls["base_url"] = base_url
        login_calls["headless"] = str(headless)
        return refreshed

    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(fake_load))
    monkeypatch.setattr(ws_auth, "_load_runtime_config", lambda account_alias=None: config)
    monkeypatch.setattr(ws_auth, "login_with_requests", fake_login)
    monkeypatch.setattr(ws_auth, "_get_auth_method", lambda config=None: "http")
    monkeypatch.setattr(
        ws_auth.WebSession,
        "save",
        lambda self, account=None: save_calls.append(account),
    )

    session = ws_auth.get_web_session(
        force_refresh=True,
        require_workspace=False,
        account="lab",
    )

    assert session is refreshed
    assert load_calls == []
    assert login_calls["username"] == "refresh-user"
    assert login_calls["password"] == "refresh-pass"
    assert login_calls["base_url"] == "https://example.invalid"
    assert session.account_alias == "lab"
    assert save_calls == ["lab"]


def test_get_web_session_persists_auth_failure_cooldown_across_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = Config(
        username="platform-user",
        password="bad-pass",
        account_alias="lab",
        base_url="https://example.invalid",
    )
    failure_path = tmp_path / "web_auth_failure-lab.json"
    login_calls = 0

    def fail_login(*_args, **_kwargs):
        nonlocal login_calls
        login_calls += 1
        raise ws.SessionAuthenticationError("CAS login failed")

    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(lambda cls, **_: None))
    monkeypatch.setattr(ws_auth, "_load_runtime_config", lambda account_alias=None: config)
    monkeypatch.setattr(ws_auth, "_get_auth_method", lambda config=None: "http")
    monkeypatch.setattr(ws_auth, "login_with_requests", fail_login)
    monkeypatch.setattr(ws_auth, "get_auth_failure_cache_file", lambda _account: failure_path)

    with pytest.raises(ws.SessionAuthenticationError, match="CAS login failed"):
        ws_auth.get_web_session(account="lab")
    with pytest.raises(ws.SessionAuthenticationError, match="authentication paused"):
        ws_auth.get_web_session(account="lab")

    payload = json.loads(failure_path.read_text())
    assert login_calls == 1
    assert payload["schema"] == "inspire.web-auth-failure.v1"
    assert 299 <= payload["retry_after"] - payload["created_at"] <= 300


def test_auth_failure_cooldown_is_longer_for_locked_accounts() -> None:
    assert ws_auth._auth_failure_cooldown_seconds("CAS account is locked") == 30 * 60
    assert ws_auth._auth_failure_cooldown_seconds("CAS login failed") == 5 * 60


def test_get_web_session_retries_after_auth_cooldown_expires(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = Config(
        username="platform-user",
        password="valid-pass",
        account_alias="lab",
        base_url="https://example.invalid",
    )
    failure_path = tmp_path / "web_auth_failure-lab.json"
    failure_path.write_text(
        json.dumps(
            {
                "schema": "inspire.web-auth-failure.v1",
                "message": "CAS login failed",
                "created_at": 1,
                "retry_after": 2,
            }
        )
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-new",
        login_username="platform-user",
        created_at=1,
    )
    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(lambda cls, **_: None))
    monkeypatch.setattr(ws_auth.WebSession, "save", lambda self, account=None: None)
    monkeypatch.setattr(ws_auth, "_load_runtime_config", lambda account_alias=None: config)
    monkeypatch.setattr(ws_auth, "_get_auth_method", lambda config=None: "http")
    monkeypatch.setattr(ws_auth, "login_with_requests", lambda *_args, **_kwargs: refreshed)
    monkeypatch.setattr(ws_auth, "get_auth_failure_cache_file", lambda _account: failure_path)

    session = ws_auth.get_web_session(account="lab")

    assert session is refreshed
    assert not failure_path.exists()


def test_web_session_round_trip_preserves_account_alias() -> None:
    session = WebSession(
        storage_state={"cookies": []},
        account_alias="lab",
        login_username="platform-user",
        created_at=1,
    )

    restored = WebSession.from_dict(session.to_dict())

    assert restored.account_alias == "lab"
    assert restored.login_username == "platform-user"


def test_clear_session_cache_removes_scoped_and_legacy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    legacy = cache_dir / "web_session.json"
    scoped = cache_dir / "web_session-project-user.json"
    auth_failure = cache_dir / "web_auth_failure-project-user.json"
    other = cache_dir / "not_session.json"
    legacy.write_text("{}")
    scoped.write_text("{}")
    auth_failure.write_text("{}")
    other.write_text("{}")

    monkeypatch.setattr(ws, "SESSION_CACHE_FILE", legacy)
    ws.clear_session_cache()

    assert not legacy.exists()
    assert not scoped.exists()
    assert not auth_failure.exists()
    assert other.exists()


def test_clear_account_session_cache_removes_only_that_accounts_datasquare_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inspire.platform.web.datasquare.session import get_datasquare_session_cache_file
    from inspire.platform.web.session import models as session_models

    monkeypatch.setattr(session_models, "SESSION_CACHE_DIR", tmp_path)
    account_paths = [ws.get_session_cache_file("lab")]
    account_paths.append(ws.get_auth_failure_cache_file("lab"))
    account_paths.append(get_datasquare_session_cache_file("lab"))
    other_paths = [ws.get_session_cache_file("other")]
    other_paths.append(ws.get_auth_failure_cache_file("other"))
    other_paths.append(get_datasquare_session_cache_file("other"))
    for path in account_paths + other_paths:
        path.write_text("{}")

    ws.clear_account_session_cache("lab")

    assert all(not path.exists() for path in account_paths)
    assert all(path.exists() for path in other_paths)


class _MockCookie:
    def __init__(self, name, value, domain, path="/", expires=None, secure=False, rest=None):
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.expires = expires
        self.secure = secure
        self._rest = rest or {}


class _MockCookieJar:
    def __init__(self, cookies):
        self._cookies = cookies

    def __iter__(self):
        return iter(self._cookies)


def test_collect_qz_cookies_skips_host_only_cookies():
    """Host-only cookies have domain=None and must not crash collection."""
    cookies = [
        _MockCookie("session", "abc123", None),
        _MockCookie("session", "def456", "qz.sii.edu.cn"),
    ]
    session = requests.Session()
    session.cookies = _MockCookieJar(cookies)

    cookie_str, expires = _collect_qz_cookies(session)

    assert cookie_str == "session=def456"
    assert expires is None


def test_collect_qz_cookies_matches_subdomains():
    """Cookies on subdomains like .sub.qz.sii.edu.cn must be collected."""
    cookies = [
        _MockCookie("session", "abc", "sub.qz.sii.edu.cn"),
        _MockCookie("other", "def", ".qz.sii.edu.cn"),
        _MockCookie("skip", "ghi", "other.com"),
    ]
    session = requests.Session()
    session.cookies = _MockCookieJar(cookies)

    cookie_str, _ = _collect_qz_cookies(session)

    assert "session=abc" in cookie_str
    assert "other=def" in cookie_str
    assert "skip=ghi" not in cookie_str


def test_requests_cookie_to_playwright_format_handles_browser_storage_boundaries():
    """Cookie conversion skips invalid cookies and preserves browser-facing attrs."""
    cookies = [
        _MockCookie("bad", "val", None),
        _MockCookie(
            "name",
            "value",
            "qz.sii.edu.cn",
            path="/jobs",
            expires=1893456000,
            secure=True,
            rest={"HttpOnly": True, "SameSite": "Lax"},
        ),
        _MockCookie("no_expiry", "value", "qz.sii.edu.cn", expires=None),
        _MockCookie("no_samesite", "value", "qz.sii.edu.cn", rest={}),
    ]
    jar = _MockCookieJar(cookies)

    result = _requests_cookie_to_playwright_format(jar)

    assert [entry["name"] for entry in result] == ["name", "no_expiry", "no_samesite"]
    entry = result[0]
    assert entry["value"] == "value"
    assert entry["domain"] == "qz.sii.edu.cn"
    assert entry["path"] == "/jobs"
    assert entry["expires"] == 1893456000
    assert entry["httpOnly"] is True
    assert entry["secure"] is True
    assert entry["sameSite"] == "Lax"
    assert "expires" not in result[1]
    assert "sameSite" not in result[2]


def test_requests_cookie_to_playwright_format_rest_none():
    """Cookies with _rest=None must not crash conversion."""
    cookie = _MockCookie("name", "value", "qz.sii.edu.cn")
    cookie._rest = None  # type: ignore[assignment]
    jar = _MockCookieJar([cookie])

    result = _requests_cookie_to_playwright_format(jar)

    assert len(result) == 1
    assert result[0]["httpOnly"] is False


def test_extract_workspaces_uses_list_workspaces_items() -> None:
    first = "ws-11111111-1111-1111-1111-111111111111"
    second = "ws-22222222-2222-2222-2222-222222222222"
    payload = {
        "Result": {
            "items": [
                {"name": "CPU", "id": first},
                {"name": "GPU", "id": second},
                {"name": "Duplicate", "id": first},
                {"name": "Invalid", "id": "user-1"},
            ]
        }
    }

    workspace_ids, workspace_names = _extract_workspaces(payload)

    assert workspace_ids == [first, second]
    assert workspace_names == {first: "CPU", second: "GPU"}


def test_build_web_session_bootstraps_workspaces_without_existing_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "ws-11111111-1111-1111-1111-111111111111"
    second = "ws-22222222-2222-2222-2222-222222222222"
    responses = {
        "https://example.invalid/api/v2/user?Action=GetUserDetail": DummyResponse(
            200,
            {
                "ResponseMetadata": {"Action": "GetUserDetail", "Service": "user"},
                "Result": {"id": "user-1"},
            },
        ),
        "https://example.invalid/api/v2/workspace?Action=ListWorkspaces": DummyResponse(
            200,
            {
                "ResponseMetadata": {"Action": "ListWorkspaces", "Service": "workspace"},
                "Result": {
                    "items": [
                        {"name": "CPU", "id": first},
                        {"name": "GPU", "id": second},
                    ]
                },
            },
        ),
    }

    class Session(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            self.requested_urls: list[str] = []

        def post(self, url, **kwargs):  # type: ignore[no-untyped-def]
            self.requested_urls.append(url)
            return responses[url]

    monkeypatch.delenv("INSPIRE_WORKSPACE_ID", raising=False)
    session = Session()
    session.cookies.set("inspire-session", "value", domain="qz.sii.edu.cn", path="/")

    result = _build_web_session(session, "platform-user", "https://example.invalid")

    assert session.requested_urls[-1].endswith("/api/v2/workspace?Action=ListWorkspaces")
    assert result.workspace_id == first
    assert result.all_workspace_ids == [first, second]
    assert result.all_workspace_names == {first: "CPU", second: "GPU"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "The account is locked. Please contact the administrator",
            "CAS account is locked",
        ),
        ("账号或密码错误", "Wrong username or password"),
        ("请输入验证码", "CAPTCHA required"),
        ("Invalid captcha", "CAPTCHA required"),
    ],
)
def test_classify_cas_login_error(body: str, message: str) -> None:
    assert message in (_classify_cas_login_error(body) or "")


def test_classify_cas_login_error_ignores_passive_captcha_page_text() -> None:
    body = "账号登录 密码登录 动态验证码 获取验证码"

    assert _classify_cas_login_error(body) is None


def test_http_auth_matches_live_cas_browser_contract(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class LoginSession(requests.Session):
        def get(self, url, **kwargs):  # type: ignore[no-untyped-def]
            if url == "https://qz.example":
                return SimpleNamespace(
                    url="https://keycloak.example/auth",
                    text='"loginUrl": "/realms/x/broker/cas/login?session=1"',
                )
            return SimpleNamespace(
                url="https://cas.sii.edu.cn/cas/login?service=qz",
                text='<input name="execution" value="execution-token">',
            )

        def post(self, url, *, data, headers, **kwargs):  # type: ignore[no-untyped-def]
            captured["url"] = url
            captured["data"] = dict(data)
            captured["headers"] = dict(headers)
            captured["session_headers"] = dict(self.headers)
            self.cookies.set(
                "inspire-session",
                "cookie-value",
                domain="qz.sii.edu.cn",
                path="/",
            )
            return SimpleNamespace(url="https://qz.sii.edu.cn/", text="")

    session = LoginSession()
    sentinel = object()
    monkeypatch.setattr(http_auth_module.requests, "Session", lambda: session)
    monkeypatch.setattr(http_auth_module, "_build_web_session", lambda *args: sentinel)

    result = http_auth_module.login_with_requests(
        "platform-user",
        "contract-password",
        base_url="https://qz.example",
    )

    assert result is sentinel
    assert captured["url"] == "https://cas.sii.edu.cn/cas/login?service=qz"
    assert set(captured["data"]) == {
        "username",
        "password",
        "execution",
        "encrypted",
        "_eventId",
        "loginType",
    }
    assert captured["data"]["password"] == http_auth_module._rsa_encrypt_password(
        "contract-password"
    )
    assert captured["data"]["execution"] == "execution-token"
    assert captured["data"]["encrypted"] == "true"
    assert captured["data"]["_eventId"] == "submit"
    assert captured["data"]["loginType"] == "1"
    assert captured["headers"]["Upgrade-Insecure-Requests"] == "1"
    assert captured["headers"]["Sec-CH-UA-Platform"] == '"Linux"'
    assert (
        f'Chromium";v="{http_auth_module._CAS_CHROMIUM_MAJOR}' in captured["headers"]["Sec-CH-UA"]
    )
    assert (
        f"HeadlessChrome/{http_auth_module._CAS_CHROMIUM_MAJOR}"
        in captured["session_headers"]["User-Agent"]
    )
    assert captured["session_headers"]["Accept-Language"].startswith("zh-CN")


def test_http_auth_raises_typed_error_when_cas_rejects_login(monkeypatch) -> None:
    class LoginSession(requests.Session):
        def get(self, url, **kwargs):  # type: ignore[no-untyped-def]
            if url == "https://qz.example":
                return SimpleNamespace(
                    url="https://keycloak.example/auth",
                    text='"loginUrl": "/realms/x/broker/cas/login?session=1"',
                )
            return SimpleNamespace(
                url="https://cas.sii.edu.cn/cas/login?service=qz",
                text='<input name="execution" value="execution-token">',
            )

        def post(self, url, **kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                url="https://cas.sii.edu.cn/cas/login?service=qz",
                text="用户名或密码错误",
            )

    monkeypatch.setattr(http_auth_module.requests, "Session", LoginSession)

    with pytest.raises(ws.SessionAuthenticationError, match="Wrong username or password"):
        http_auth_module.login_with_requests(
            "platform-user",
            "contract-password",
            base_url="https://qz.example",
        )


def test_is_encrypted_short_hex_passwords_get_encrypted():
    """Short hex-like passwords must be encrypted, not treated as already encrypted."""
    from inspire.platform.web.session.http_auth import _rsa_encrypt_password

    # Short hex passwords should be encrypted (not treated as already encrypted)
    short_hex_passwords = ["deadbeef", "cafebabe", "1234567890abcdef"]
    for pw in short_hex_passwords:
        encrypted = _rsa_encrypt_password(pw)
        assert encrypted != pw
        assert len(encrypted) >= 100
        assert all(c in "0123456789abcdefABCDEF " for c in encrypted)


def test_is_encrypted_long_hex_password_unchanged():
    """A genuinely long hex string (already encrypted) should be returned as-is."""
    from inspire.platform.web.session.http_auth import _rsa_encrypt_password

    # First encrypt a password to get a valid encrypted form
    encrypted = _rsa_encrypt_password("my_password")
    # Now pass it back through - it should be idempotent
    result = _rsa_encrypt_password(encrypted)
    assert result == encrypted


def test_collect_qz_cookies_extracts_expires():
    """Session cookie with an expires timestamp must be returned."""
    cookies = [
        _MockCookie("my_session", "val", "qz.sii.edu.cn", expires=1893456000),
    ]
    session = requests.Session()
    session.cookies = _MockCookieJar(cookies)

    cookie_str, expires_at = _collect_qz_cookies(session)

    assert cookie_str == "my_session=val"
    assert expires_at == 1893456000


def test_collect_qz_cookies_no_session_fallback():
    """When no *session cookie exists, return None, None."""
    cookies = [
        _MockCookie("other", "val", "qz.sii.edu.cn"),
    ]
    session = requests.Session()
    session.cookies = _MockCookieJar(cookies)

    cookie_str, expires_at = _collect_qz_cookies(session)

    assert cookie_str is None
    assert expires_at is None


def test_login_with_playwright_uses_the_shared_chromium_launch_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login must launch Chromium the same way every other browser command does (#137).

    Headless containers cannot start Chromium without ``--no-sandbox``; a direct
    ``p.chromium.launch`` here bypassed the shared flags and broke first login.
    """
    import sys
    import types

    from inspire.platform.web.browser_api import core as browser_core

    launch_kwargs: dict[str, object] = {}

    class _StopAfterLaunch(RuntimeError):
        pass

    class _FakeContext:
        def new_page(self) -> object:
            raise _StopAfterLaunch("stop after page creation")

    class _FakeBrowser:
        def new_context(self, **_kwargs: object) -> _FakeContext:
            return _FakeContext()

    class _FakeChromium:
        def launch(self, **kwargs: object) -> _FakeBrowser:
            launch_kwargs.update(kwargs)
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        def __enter__(self) -> "_FakePlaywright":
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _FakePlaywright()  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.__path__ = []  # type: ignore[attr-defined]
    package.sync_api = sync_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(browser_core, "get_playwright_proxy", lambda: None)

    with pytest.raises(_StopAfterLaunch):
        ws_auth.login_with_playwright("user", "password", base_url="https://api.example")

    args = launch_kwargs.get("args") or []
    assert launch_kwargs["headless"] is True
    assert "--no-sandbox" in args
    assert "--disable-setuid-sandbox" in args
    assert "--disable-dev-shm-usage" in args
