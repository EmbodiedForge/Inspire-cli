from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.config import Config, ConfigError
from inspire.config.account_state import (
    AccountDiscoveryState,
    Observation,
    get_account_discovery_state_path,
    save_account_discovery_state,
)
from inspire.config import load_runtime


def _global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "global" / "config.toml"
    monkeypatch.setenv("INSPIRE_GLOBAL_CONFIG_PATH", str(path))
    monkeypatch.delenv("INSPIRE_ACCOUNT", raising=False)
    monkeypatch.delenv("INSPIRE_PASSWORD", raising=False)
    monkeypatch.delenv("INSPIRE_USERNAME", raising=False)
    return path


def _complete_state(alias: str = "lab") -> AccountDiscoveryState:
    workspace_id = "ws-11111111-1111-1111-1111-111111111111"
    return AccountDiscoveryState(
        account_alias=alias,
        platform_username=f"{alias}-user",
        base_url="https://qz.example",
        workspaces=Observation.success([{"id": workspace_id, "name": "CPU Workspace"}]),
        projects={
            workspace_id: Observation.success(
                [{"id": "project-11111111-1111-1111-1111-111111111111", "name": "Training"}]
            )
        },
        compute_groups={workspace_id: Observation.success([])},
        workspace_specs={workspace_id: Observation.success([])},
    )


def test_account_catalog_register_list_and_remove_preserve_other_tables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("# keep this comment\n[defaults]\npriority = 3\n", encoding="utf-8")
    runner = CliRunner()

    registered = runner.invoke(
        cli_main,
        [
            "config",
            "account",
            "register",
            "lab",
            "--username",
            "platform-user",
            "--base-url",
            "https://qz.example/",
            "--password-command",
            "secret-tool lookup inspire lab",
        ],
    )
    assert registered.exit_code == 0, registered.output
    text = path.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "priority = 3" in text
    assert "[accounts.lab]" in text
    assert 'username = "platform-user"' in text

    listed = runner.invoke(cli_main, ["--json", "config", "account", "list"])
    assert listed.exit_code == 0, listed.output
    account = json.loads(listed.output)["data"]["accounts"][0]
    assert account == {
        "alias": "lab",
        "username": "platform-user",
        "base_url": "https://qz.example",
        "auth_method": "http",
        "credential": "password_command",
    }
    assert "secret-tool" not in listed.output

    removed = runner.invoke(cli_main, ["config", "account", "remove", "lab", "--yes"])
    assert removed.exit_code == 0, removed.output
    assert "priority = 3" in path.read_text(encoding="utf-8")


def test_account_register_force_reports_effective_merged_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        '[accounts.lab]\nusername = "old-user"\nbase_url = "https://qz.example"\n'
        'password_command = "provider lookup lab"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "config",
            "account",
            "register",
            "lab",
            "--username",
            "new-user",
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["username"] == "new-user"
    assert payload["base_url"] == "https://qz.example"
    assert payload["credential"] == "password_command"
    assert "provider lookup lab" not in result.output


def test_account_show_and_export_never_reveal_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        '[accounts.lab]\nusername = "lab-user"\npassword = "plain-secret"\n'
        'password_command = "provider secret-command"\n\n'  # pragma: allowlist secret
        '[accounts.lab.workspaces.cpu]\nname = "CPU Workspace"\n',
        encoding="utf-8",
    )
    save_account_discovery_state(_complete_state())
    runner = CliRunner()

    shown = runner.invoke(cli_main, ["config", "account", "show", "lab"])
    assert shown.exit_code == 0, shown.output
    assert "Account: lab" in shown.output
    assert "password_command" in shown.output
    assert "plain-secret" not in shown.output
    assert "secret-command" not in shown.output

    shown_json = runner.invoke(cli_main, ["--json", "config", "account", "show", "lab"])
    assert shown_json.exit_code == 0, shown_json.output
    show_payload = json.loads(shown_json.output)["data"]
    assert show_payload["state"]["status"] == "complete"
    assert show_payload["workspaces"]["cpu"]["name"] == "CPU Workspace"
    assert "plain-secret" not in shown_json.output
    assert "secret-command" not in shown_json.output

    exported = runner.invoke(cli_main, ["--json", "config", "account", "export", "lab"])
    assert exported.exit_code == 0, exported.output
    export_payload = json.loads(exported.output)["data"]["accounts"]["lab"]
    assert export_payload["username"] == "lab-user"
    assert "password" not in export_payload
    assert "password_command" not in export_payload


def test_account_doctor_checks_state_and_live_exact_selectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        '[accounts.lab]\nusername = "lab-user"\n\n'
        '[accounts.lab.workspaces.cpu]\nname = "CPU Workspace"\n\n'
        '[accounts.lab.projects.training]\nworkspace = "cpu"\nname = "Training"\n',
        encoding="utf-8",
    )
    save_account_discovery_state(_complete_state())
    workspace_id = "ws-11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(
        "inspire.platform.web.session.get_web_session",
        lambda **kwargs: SimpleNamespace(workspace_id=workspace_id, base_url="https://qz.example"),
    )
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.workspaces.try_enumerate_workspaces",
        lambda *args, **kwargs: [{"id": workspace_id, "name": "CPU Workspace"}],
    )
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.list_projects",
        lambda **kwargs: [SimpleNamespace(project_id="project-1", name="Training")],
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "config", "account", "doctor", "lab", "--live"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["ok"] is True
    assert payload["issues"] == []
    assert payload["live"] == {
        "workspace_count": 1,
        "resolved_workspace_selectors": 1,
        "project_selectors_checked": 1,
    }


@pytest.mark.parametrize("state_kind", ["missing", "partial"])
def test_account_doctor_fails_for_missing_or_partial_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state_kind: str
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('[accounts.lab]\nusername = "lab-user"\n', encoding="utf-8")
    if state_kind == "partial":
        state = _complete_state()
        state.projects[next(iter(state.projects))] = Observation.failed("project API unavailable")
        save_account_discovery_state(state)

    result = CliRunner().invoke(cli_main, ["config", "account", "doctor", "lab"])

    assert result.exit_code != 0
    assert "FAILED" in result.output
    assert (
        f"discovery state {'is missing' if state_kind == 'missing' else 'status is partial'}"
        in result.output
    )


def test_account_remove_clears_only_target_state_and_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        '# preserve\n[accounts.lab]\nusername = "lab-user"\n\n'
        '[accounts.personal]\nusername = "personal-user"\n',
        encoding="utf-8",
    )
    save_account_discovery_state(_complete_state("lab"))
    save_account_discovery_state(_complete_state("personal"))
    cleared: list[str] = []
    monkeypatch.setattr(
        "inspire.platform.web.session.clear_account_session_cache",
        lambda alias: cleared.append(alias),
    )

    result = CliRunner().invoke(cli_main, ["config", "account", "remove", "lab", "--yes"])

    assert result.exit_code == 0, result.output
    content = path.read_text(encoding="utf-8")
    assert "# preserve" in content
    assert "accounts.personal" in content
    assert "accounts.lab" not in content
    assert not get_account_discovery_state_path("lab").exists()
    assert get_account_discovery_state_path("personal").exists()
    assert cleared == ["lab"]


def test_selected_account_password_precedence_command_then_plaintext_then_env_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    command = f"{sys.executable} -c \"print('command-password')\""
    path.parent.mkdir(parents=True)
    path.write_text(
        '[context]\naccount = "lab"\n\n'
        "[accounts.lab]\n"
        'username = "platform-user"\n'
        'password = "plaintext-password"\n'
        f"password_command = {json.dumps(command)}\n",
        encoding="utf-8",
    )

    config, _ = Config.from_files_and_env()
    assert config.password == "command-password"

    monkeypatch.setenv("INSPIRE_PASSWORD", "environment-password")
    config, _ = Config.from_files_and_env()
    assert config.password == "command-password"

    monkeypatch.delenv("INSPIRE_PASSWORD")
    text = path.read_text(encoding="utf-8").replace(
        f"password_command = {json.dumps(command)}\n", ""
    )
    path.write_text(text, encoding="utf-8")
    config, _ = Config.from_files_and_env()
    assert config.password == "plaintext-password"

    text = path.read_text(encoding="utf-8").replace('password = "plaintext-password"\n', "")
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("INSPIRE_PASSWORD", "environment-password")
    config, sources = Config.from_files_and_env()
    assert config.password == "environment-password"
    assert sources["password"] == "env"


def test_explicit_account_ignores_ambient_password_when_catalog_has_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        "[accounts.lab]\n" 'username = "platform-user"\n' 'password = "account-password"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("INSPIRE_PASSWORD", "polluted-shell-password")

    config, sources = Config.from_files_and_env(account_alias="lab")

    assert config.account_alias == "lab"
    assert config.password == "account-password"
    assert sources["password"] == "global"


def test_account_list_does_not_guess_password_storage(monkeypatch, tmp_path) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        "[accounts.lab]\n"
        'username = "platform-user"\n'
        'password = "<external-secret-reference>"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["--json", "config", "account", "list"])

    assert result.exit_code == 0, result.output
    account = json.loads(result.output)["data"]["accounts"][0]
    assert account["credential"] == "configured_password"
    assert "external-secret-reference" not in result.output


def test_password_command_uses_no_shell_timeout_and_redacted_failure(monkeypatch) -> None:
    captured = {}

    def fake_run(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=7, stdout="", stderr="sensitive backend detail")

    monkeypatch.setattr(load_runtime.subprocess, "run", fake_run)
    with pytest.raises(ConfigError, match="failed with exit code 7") as exc_info:
        load_runtime._run_password_command("provider --account lab")

    assert captured["argv"] == ["provider", "--account", "lab"]
    assert captured["shell"] is False
    assert captured["timeout"] == 5
    assert "sensitive backend detail" not in str(exc_info.value)


def test_password_command_timeout_is_redacted(monkeypatch) -> None:
    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
        raise subprocess.TimeoutExpired(cmd="provider --secret value", timeout=5)

    monkeypatch.setattr(load_runtime.subprocess, "run", fake_run)
    with pytest.raises(ConfigError, match="password_command timed out") as exc_info:
        load_runtime._run_password_command("provider --secret value")
    assert "value" not in str(exc_info.value)


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "piped-stdin"])
def test_account_remove_noninteractive_refusal_preserves_config_and_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    json_mode: bool,
) -> None:
    path = _global_config(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('[accounts.lab]\nusername = "lab-user"\n', encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    cleared: list[str] = []
    monkeypatch.setattr(
        "inspire.cli.commands.config.account.clear_account_discovery_state",
        lambda alias: cleared.append(f"state:{alias}"),
    )
    monkeypatch.setattr(
        "inspire.platform.web.session.clear_account_session_cache",
        lambda alias: cleared.append(f"session:{alias}"),
    )
    monkeypatch.setattr(
        "inspire.cli.utils.confirm.click.confirm",
        lambda *args, **kwargs: pytest.fail("noninteractive account removal prompted"),
    )

    argv = ["config", "account", "remove", "lab"]
    result = CliRunner().invoke(
        cli_main,
        ["--json", *argv] if json_mode else argv,
        input=None if json_mode else "y\n",
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert result.stdout == ""
    assert path.read_text(encoding="utf-8") == original
    assert cleared == []
    if json_mode:
        assert json.loads(result.stderr) == {
            "success": False,
            "error": {
                "type": "ValidationError",
                "code": EXIT_VALIDATION_ERROR,
                "message": (
                    "Refusing to remove account alias 'lab' and its catalog state without "
                    "confirmation. Pass --yes to run it without an interactive prompt."
                ),
                "hint": (
                    "In a script or agent run, --yes is the confirmation; "
                    "leave it out to stop here."
                ),
            },
        }
    else:
        assert "Error: Refusing to remove account alias 'lab'" in result.stderr
