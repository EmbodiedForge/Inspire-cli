from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from inspire.platform.web.datasquare.session import (
    get_datasquare_session,
    save_datasquare_session,
)
from inspire.platform.web.session import models as session_models
from inspire.platform.web.session.auth import get_web_session
from inspire.platform.web.session.models import SessionAuthenticationError, WebSession

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "debug_web_session.py"
TEST_ACCOUNT = "test-account"


class _DummyResponse:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {"code": 0, "message": "success"}


class _DummyV2Response:
    status_code = 200
    text = ""

    def __init__(self, user: dict) -> None:
        self.user = user

    def json(self) -> dict:
        return {
            "ResponseMetadata": {"Service": "user", "Action": "GetUserDetail"},
            "Result": self.user,
        }


class _DummyHTTP:
    def __init__(self, user: dict | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False
        self.user = user or {"id": 1, "username": TEST_ACCOUNT}

    def get(self, url: str, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(("GET", url))
        return _DummyResponse()

    def post(self, url: str, headers=None, json=None, timeout=None):  # noqa: ANN001
        # Session verification is a v2 Action call, not a v1 REST GET (#84).
        self.calls.append(("POST", url))
        return _DummyV2Response(self.user)

    def close(self) -> None:
        self.closed = True


def _load_script_module():
    spec = importlib.util.spec_from_file_location("debug_web_session_script", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_session_cache(monkeypatch, tmp_path: Path) -> Path:  # noqa: ANN001
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(session_models, "SESSION_CACHE_DIR", cache_dir)
    monkeypatch.setattr(session_models, "SESSION_CACHE_FILE", cache_dir / "web_session.json")
    return cache_dir


def _write_config(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    monkeypatch.delenv("INSPIRE_USERNAME", raising=False)
    monkeypatch.delenv("INSPIRE_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[auth]",
                f'username = "{TEST_ACCOUNT}"',
                "",
                "[api]",
                'base_url = "https://qz.sii.edu.cn"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("INSPIRE_GLOBAL_CONFIG_PATH", str(config_path))


def _storage_state() -> dict:
    return {
        "cookies": [
            {
                "name": "session",
                "value": "cookie-value",
                "domain": "qz.sii.edu.cn",
                "path": "/",
            }
        ],
        "origins": [
            {
                "origin": "https://qz.sii.edu.cn",
                "localStorage": [
                    {"name": "spaceId", "value": "ws-test"},
                ],
            }
        ],
    }


def test_debug_web_session_import_state_saves_account_scoped_cache(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    debug_web_session = _load_script_module()
    _patch_session_cache(monkeypatch, tmp_path)
    _write_config(monkeypatch, tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_storage_state()), encoding="utf-8")
    http = _DummyHTTP()
    monkeypatch.setattr(debug_web_session, "build_requests_session", lambda _session, _url: http)

    exit_code = debug_web_session.main(["import-state", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["data"]["account"] == TEST_ACCOUNT
    assert payload["data"]["workspace_id"] == "ws-test"
    assert payload["data"]["verification"]["ok"] is True
    saved = WebSession.load(allow_expired=True, account=TEST_ACCOUNT)
    assert saved is not None
    assert saved.login_username == TEST_ACCOUNT
    assert saved.workspace_id == "ws-test"
    assert http.closed is True


@pytest.mark.parametrize("username_matches", [True, False])
def test_debug_web_session_import_selects_and_verifies_account_alias(
    tmp_path: Path, monkeypatch, capsys, username_matches: bool
) -> None:
    debug_web_session = _load_script_module()
    cache_dir = _patch_session_cache(monkeypatch, tmp_path)
    _write_config(monkeypatch, tmp_path)
    username = TEST_ACCOUNT if username_matches else "other-platform-user"
    (tmp_path / "config.toml").write_text(
        f'[accounts.main]\nusername = "{username}"\n'
        '[accounts.backup]\nusername = "backup-user"\n'
        '[api]\nbase_url = "https://qz.sii.edu.cn"\n',
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_storage_state()), encoding="utf-8")
    http = _DummyHTTP({"name": "Display name", "extra_info": {"login_name": TEST_ACCOUNT}})
    monkeypatch.setattr(debug_web_session, "build_requests_session", lambda *_: http)

    exit_code = debug_web_session.main(["import-state", str(state_path), "--account", "main"])

    if not username_matches:
        assert exit_code == 13
        assert "does not match the configured username" in capsys.readouterr().err
        assert not (cache_dir / "web_session-main.json").exists()
        return

    assert exit_code == 0
    session = get_web_session(account="main")
    assert session.account_alias == "main"
    assert session.login_username == TEST_ACCOUNT
    assert session.workspace_id == "ws-test"
    assert debug_web_session.main(["check", "--account", "main"]) == 0


@pytest.mark.parametrize("outcome", ["refreshed", "expired", "replaced", "removed"])
def test_debug_web_session_refresh_by_sso_preserves_cache_consistency(
    tmp_path: Path, monkeypatch, capsys, outcome: str
) -> None:
    debug_web_session = _load_script_module()
    _patch_session_cache(monkeypatch, tmp_path)
    _write_config(monkeypatch, tmp_path)
    original = WebSession(
        storage_state=_storage_state(),
        login_username=TEST_ACCOUNT,
        base_url="https://qz.sii.edu.cn",
        created_at=1.0,
    )
    original.save()

    def refresh(session: WebSession, base_url: str) -> WebSession:
        assert session.to_dict() == original.to_dict()
        assert base_url == original.base_url
        if outcome == "expired":
            raise SessionAuthenticationError("SSO expired")
        if outcome == "replaced":
            replace(original, created_at=3.0).save()
        if outcome == "removed":
            session_models.get_session_cache_file(TEST_ACCOUNT).unlink()
        return replace(original, created_at=2.0)

    monkeypatch.setattr(debug_web_session, "refresh_with_requests", refresh)
    exit_code = debug_web_session.main(["refresh", "--account", TEST_ACCOUNT])
    saved = WebSession.load(allow_expired=True, account=TEST_ACCOUNT)
    output = capsys.readouterr()

    if outcome == "expired":
        assert exit_code == 13
        assert "re-import" in output.err
    else:
        assert exit_code == 0
        assert json.loads(output.out)["data"]["refreshed"] is (outcome == "refreshed")
    expected = {"refreshed": 2.0, "expired": 1.0, "replaced": 3.0, "removed": None}
    assert (saved.created_at if saved else None) == expected[outcome]


def test_debug_web_session_check_uses_cached_session_without_login(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    debug_web_session = _load_script_module()
    _patch_session_cache(monkeypatch, tmp_path)
    _write_config(monkeypatch, tmp_path)
    WebSession(
        storage_state=_storage_state(),
        cookies={"session": "cookie-value"},
        workspace_id="ws-test",
        login_username=TEST_ACCOUNT,
        base_url="https://qz.sii.edu.cn",
        created_at=1.0,
    ).save(account=TEST_ACCOUNT)
    http = _DummyHTTP()
    monkeypatch.setattr(debug_web_session, "build_requests_session", lambda _session, _url: http)

    exit_code = debug_web_session.main(["check"])

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["data"]["verification"]["ok"] is True
    assert payload["data"]["cookie_count"] == 1
    assert http.calls == [("POST", "https://qz.sii.edu.cn/api/v2/user?Action=GetUserDetail")]


DATASQUARE_STATE_COOKIE = {
    "name": "datasets-session",
    "value": "jwt-value",
    "domain": "aip.sii.edu.cn",
    "path": "/",
}


class _DummyDatasquareResponse:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {"code": 0, "msg": "success", "data": {"total": 1, "list": [{"datasetId": 1}]}}


class _DummyDatasquareHTTP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def get(self, url: str, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(("GET", url))
        return _DummyDatasquareResponse()

    def close(self) -> None:
        self.closed = True


def _datasquare_storage_state() -> dict:
    return {
        "cookies": [DATASQUARE_STATE_COOKIE],
        "origins": [
            {
                "origin": "https://aip.sii.edu.cn",
                "localStorage": [{"name": "spaceId", "value": "should-be-ignored"}],
            }
        ],
    }


def _prepare_datasquare_run(monkeypatch, tmp_path: Path, debug_web_session):  # noqa: ANN001
    _patch_session_cache(monkeypatch, tmp_path)
    _write_config(monkeypatch, tmp_path)
    monkeypatch.delenv("INSPIRE_DATASQUARE_BASE_URL", raising=False)
    http = _DummyDatasquareHTTP()
    monkeypatch.setattr(debug_web_session, "build_requests_session", lambda _session, _url: http)
    return http


def test_debug_web_session_import_state_aip_writes_datasquare_cache_only(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    debug_web_session = _load_script_module()
    http = _prepare_datasquare_run(monkeypatch, tmp_path, debug_web_session)
    state_path = tmp_path / "aip-state.json"
    state_path.write_text(json.dumps(_datasquare_storage_state()), encoding="utf-8")

    exit_code = debug_web_session.main(["import-state", str(state_path), "--portal", "aip"])

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["data"]["verification"]["ok"] is True
    # The catalogue read is the cheapest call that proves the portal accepts the cookie.
    assert http.calls == [
        ("GET", "https://aip.sii.edu.cn/api/datasets/getDatasetsList?page=1&pageSize=1")
    ]
    # The data square has no workspace, and its cache must not touch the compute platform's.
    assert payload["data"]["workspace_id"] is None
    datasquare_file = tmp_path / "cache" / f"datasquare_session-{TEST_ACCOUNT}.json"
    assert Path(payload["data"]["cache_file"]) == datasquare_file
    assert datasquare_file.exists()
    assert not (tmp_path / "cache" / f"web_session-{TEST_ACCOUNT}.json").exists()
    saved = get_datasquare_session(account=TEST_ACCOUNT)
    assert saved is not None
    assert saved.login_username == TEST_ACCOUNT
    assert saved.base_url == "https://aip.sii.edu.cn"


def test_debug_web_session_check_aip_loads_datasquare_cache(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    debug_web_session = _load_script_module()
    http = _prepare_datasquare_run(monkeypatch, tmp_path, debug_web_session)
    save_datasquare_session(
        WebSession(
            storage_state=_datasquare_storage_state(),
            cookies={"datasets-session": "jwt-value"},
            workspace_id=None,
            login_username=TEST_ACCOUNT,
            base_url="https://aip.sii.edu.cn",
            created_at=1.0,
        ),
        account=TEST_ACCOUNT,
    )

    exit_code = debug_web_session.main(["check", "--portal", "aip"])

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["data"]["verification"]["ok"] is True
    assert payload["data"]["cookie_count"] == 1
    # A past-TTL cache is still offered up, because only the portal knows the cookie is dead.
    assert payload["data"]["age_seconds"] > 0
    assert http.calls == [
        ("GET", "https://aip.sii.edu.cn/api/datasets/getDatasetsList?page=1&pageSize=1")
    ]


def test_debug_web_session_check_aip_reports_envelope_code_and_missing_cache(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    debug_web_session = _load_script_module()
    http = _prepare_datasquare_run(monkeypatch, tmp_path, debug_web_session)

    # No datasquare cache written: the hint must name the portal's own login command.
    exit_code = debug_web_session.main(["check", "--portal", "aip"])
    captured = capsys.readouterr()
    assert exit_code == 10
    assert "--portal aip" in json.loads(captured.err)["error"]["message"]
    assert http.calls == []

    class _UnauthorizedResponse:
        status_code = 200
        text = ""

        def json(self) -> dict:
            return {"code": 7, "msg": "not logged in"}

    class _UnauthorizedHTTP(_DummyDatasquareHTTP):
        def get(self, url: str, headers=None, timeout=None):  # noqa: ANN001
            self.calls.append(("GET", url))
            return _UnauthorizedResponse()

    save_datasquare_session(
        WebSession(
            storage_state=_datasquare_storage_state(),
            cookies={"datasets-session": "jwt-value"},
            created_at=1.0,
        ),
        account=TEST_ACCOUNT,
    )
    monkeypatch.setattr(
        debug_web_session, "build_requests_session", lambda _session, _url: _UnauthorizedHTTP()
    )

    exit_code = debug_web_session.main(["check", "--portal", "aip"])
    captured = capsys.readouterr()
    # HTTP 200 with envelope code 7 is an auth finding, not a pass.
    assert exit_code == 13
    assert "not logged in" in json.loads(captured.err)["error"]["message"]
