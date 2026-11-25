"""`config check` auth surface: CAS web session is authoritative, /auth/token is opt-in.

The platform moved to /api/v1/* + web-session auth; the legacy password-token
endpoint is no longer the primary login path, so it must not be able to declare
a working installation broken.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire.cli.context import EXIT_AUTH_ERROR, EXIT_SUCCESS
from inspire.cli.main import main as cli_main

TEST_DOCKER_REGISTRY = "registry.local"


def stub_legacy_openapi(
    monkeypatch: pytest.MonkeyPatch, *, ok: bool = True, error: str = ""
) -> None:
    from inspire.cli.utils import auth as auth_module

    if ok:
        monkeypatch.setattr(auth_module.AuthManager, "get_api", lambda _cls, cfg=None: object())
    else:

        def boom(_cls, cfg=None):  # noqa: ANN001, ANN003
            from inspire.cli.utils.auth import AuthenticationError

            raise AuthenticationError(error)

        monkeypatch.setattr(auth_module.AuthManager, "get_api", boom)


def run_config_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    args: list[str] | None = None,
    *,
    json_output: bool = False,
    debug: bool = False,
):
    """Invoke `config check`; note `--json` is a top-level flag by design."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("INSPIRE_GLOBAL_CONFIG_PATH", str(tmp_path / "global.toml"))

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        config_dir = Path.cwd() / ".inspire"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            f"""
[cli]
prefer_source = "toml"

[auth]
username = "user"
password = "pass"

[api]
base_url = "https://example.invalid"
docker_registry = "{TEST_DOCKER_REGISTRY}"
""",
            encoding="utf-8",
        )
        argv = ["config", "check", *(args or [])]
        if json_output:
            argv.insert(0, "--json")
        if debug:
            argv.insert(0, "--debug")
        return runner.invoke(cli_main, argv)


def test_check_passes_on_web_session_without_legacy_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_web_session
) -> None:
    stub_web_session(ok=True, identity="谌奕同")

    result = run_config_check(monkeypatch, tmp_path, json_output=True)

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["data"]["auth_ok"] is True
    assert payload["data"]["auth_method"] == "web_session"
    assert payload["data"]["auth"]["web_session"]["ok"] is True
    assert payload["data"]["auth"]["web_session"]["identity"] == "谌奕同"
    assert payload["data"]["auth"]["legacy_openapi"]["probed"] is False


def test_check_reports_web_session_failure_as_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_web_session
) -> None:
    stub_web_session(ok=False, error="CAS login returned 401")

    result = run_config_check(monkeypatch, tmp_path)

    assert result.exit_code == EXIT_AUTH_ERROR
    assert "Authentication failed (platform web session)" in result.output
    assert "web_session-" in result.output  # points at the stale cache file to clear


def test_check_json_auth_failure_uses_stderr_error_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_web_session
) -> None:
    stub_web_session(ok=False, error="CAS login returned 401")

    result = run_config_check(monkeypatch, tmp_path, json_output=True)

    assert result.exit_code == EXIT_AUTH_ERROR
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["success"] is False
    assert payload["error"]["type"] == "AuthenticationError"
    assert payload["error"]["code"] == EXIT_AUTH_ERROR
    assert "platform web session" in payload["error"]["message"]
    assert payload["data"]["auth_ok"] is False
    assert payload["data"]["auth_error"] == "CAS login returned 401"
    assert payload["data"]["auth"]["web_session"]["ok"] is False


def test_legacy_token_failure_does_not_break_a_working_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_web_session
) -> None:
    """The regression this guards: /auth/token rejected while everything works."""
    stub_web_session(ok=True)
    stub_legacy_openapi(
        monkeypatch,
        ok=False,
        error="401 Unauthorized: invalid_grant: Invalid user credentials",
    )

    result = run_config_check(monkeypatch, tmp_path)

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Configuration looks good" in result.output
    assert "invalid_grant" not in result.output

    # The auth surface summary is diagnostic output, so it appears with --debug.
    debug_result = run_config_check(monkeypatch, tmp_path, debug=True)
    assert debug_result.exit_code == EXIT_SUCCESS, debug_result.output
    assert "Auth (web session /api/v1): OK" in debug_result.output
    assert "Auth (legacy /auth/token):  not probed" in debug_result.output


def test_require_openapi_gates_on_legacy_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_web_session
) -> None:
    stub_web_session(ok=True)
    stub_legacy_openapi(monkeypatch, ok=True)

    result = run_config_check(monkeypatch, tmp_path, ["--require-openapi"], json_output=True)

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["data"]["auth"]["legacy_openapi"] == {
        "probed": True,
        "ok": True,
        "error": None,
        "identity": None,
    }


def test_require_openapi_failure_keeps_password_shape_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_web_session
) -> None:
    stub_web_session(ok=True)
    stub_legacy_openapi(
        monkeypatch,
        ok=False,
        error="401 Unauthorized: invalid_grant: Invalid user credentials",
    )

    result = run_config_check(monkeypatch, tmp_path, ["--require-openapi"], json_output=True)

    assert result.exit_code == EXIT_AUTH_ERROR
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["success"] is False
    assert payload["error"]["code"] == EXIT_AUTH_ERROR
    assert payload["data"]["auth"]["web_session"]["ok"] is True
    assert "OpenAPI password-token login rejected these credentials" in payload["data"]["auth_hint"]
    assert payload["data"]["auth"]["legacy_openapi"]["ok"] is False
