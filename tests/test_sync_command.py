import importlib
import json
from pathlib import Path
from typing import Any, Dict

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.context import EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, EXIT_SUCCESS
from inspire.cli.main import main as cli_main
from inspire.config import Config

sync_cmd_module = importlib.import_module("inspire.cli.commands.sync")


def make_sync_config(tmp_path: Path) -> Config:
    return Config(
        username="",
        password="",
        target_dir=str(tmp_path),
        default_remote="origin",
        tunnel_retries=0,
        tunnel_retry_pause=0.0,
    )


def make_tunnel_config() -> TunnelConfig:
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="cpu-bridge",
            proxy_url="https://bridge.example.com",
            has_internet=True,
        )
    )
    return tunnel_config


def make_mixed_tunnel_config(*, default_bridge: str = "gpu-main") -> TunnelConfig:
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://gpu.example.com",
            has_internet=True,
        )
    )
    tunnel_config.add_bridge(
        BridgeProfile(
            name="cpu-main",
            proxy_url="https://cpu.example.com",
            has_internet=True,
        )
    )
    tunnel_config.default_bridge = default_bridge
    return tunnel_config


def make_gpu_only_no_internet_tunnel_config() -> TunnelConfig:
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-offline",
            proxy_url="https://gpu-offline.example.com",
            has_internet=False,
        )
    )
    return tunnel_config


def _patch_common_git_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_cmd_module, "get_current_branch", lambda: "main")
    monkeypatch.setattr(sync_cmd_module, "get_current_commit_sha", lambda revision="HEAD": "a" * 40)
    monkeypatch.setattr(
        sync_cmd_module, "get_commit_message", lambda revision="HEAD": "test commit"
    )
    monkeypatch.setattr(sync_cmd_module, "has_uncommitted_changes", lambda: False)


def test_sync_ssh_preflight_happens_before_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    push_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: push_called.update(value=True),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert push_called["value"] is False


def test_sync_workflow_preflight_happens_before_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    push_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: push_called.update(value=True),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--transport", "workflow"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert push_called["value"] is False


def test_sync_force_requires_ssh_transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)
    preflight_called = {"value": False}
    push_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(sync_cmd_module, "get_current_branch", lambda: "main")
    monkeypatch.setattr(
        sync_cmd_module,
        "_preflight_workflow_transport",
        lambda *_args, **_kwargs: preflight_called.update(value=True),
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: push_called.update(value=True),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--transport", "workflow", "--force"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "--force is only supported" in result.output
    assert preflight_called["value"] is False
    assert push_called["value"] is False


def test_sync_ssh_passes_remote_to_tunnel_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    def fake_sync_via_ssh(*args: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"success": True, "synced_sha": "a" * 40, "error": None}

    monkeypatch.setattr(sync_cmd_module, "sync_via_ssh", fake_sync_via_ssh)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push", "--remote", "upstream"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["remote"] == "upstream"
    assert captured["commit_sha"] == "a" * 40
    assert captured["force"] is False


def test_sync_ssh_prefers_cpu_bridge_over_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}
    checked_bridges: list[str] = []

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(
        sync_cmd_module,
        "load_tunnel_config",
        lambda: make_mixed_tunnel_config(default_bridge="gpu-main"),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        checked_bridges.append(kwargs["bridge_name"])
        return True

    def fake_sync_via_ssh(*args: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"success": True, "synced_sha": "a" * 40, "error": None}

    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(sync_cmd_module, "sync_via_ssh", fake_sync_via_ssh)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push"])

    assert result.exit_code == EXIT_SUCCESS
    assert checked_bridges[0] == "cpu-main"
    assert captured["bridge_name"] == "cpu-main"


def test_sync_ssh_falls_back_when_cpu_bridge_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}
    checked_bridges: list[str] = []

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(
        sync_cmd_module,
        "load_tunnel_config",
        lambda: make_mixed_tunnel_config(default_bridge="gpu-main"),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        bridge_name = kwargs["bridge_name"]
        checked_bridges.append(bridge_name)
        return bridge_name == "gpu-main"

    def fake_sync_via_ssh(*args: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"success": True, "synced_sha": "a" * 40, "error": None}

    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(sync_cmd_module, "sync_via_ssh", fake_sync_via_ssh)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push"])

    assert result.exit_code == EXIT_SUCCESS
    assert checked_bridges[:2] == ["cpu-main", "gpu-main"]
    assert captured["bridge_name"] == "gpu-main"


def test_sync_ssh_uses_offline_bundle_when_only_no_internet_bridge_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    bundle_captured: Dict[str, Any] = {}
    ssh_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(
        sync_cmd_module, "load_tunnel_config", make_gpu_only_no_internet_tunnel_config
    )
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    def fake_sync_via_ssh(*args: Any, **kwargs: Any) -> dict:
        ssh_called["value"] = True
        return {"success": False, "synced_sha": None, "error": "should not be called"}

    def fake_sync_via_ssh_bundle(*args: Any, **kwargs: Any) -> dict:
        bundle_captured.update(kwargs)
        return {"success": True, "synced_sha": "a" * 40, "error": None}

    monkeypatch.setattr(sync_cmd_module, "sync_via_ssh", fake_sync_via_ssh)
    monkeypatch.setattr(sync_cmd_module, "sync_via_ssh_bundle", fake_sync_via_ssh_bundle)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push"])

    assert result.exit_code == EXIT_SUCCESS
    assert ssh_called["value"] is False
    assert bundle_captured["bridge_name"] == "gpu-offline"


def test_sync_source_remote_requires_internet_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    ssh_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(
        sync_cmd_module, "load_tunnel_config", make_gpu_only_no_internet_tunnel_config
    )
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh",
        lambda *args, **kwargs: ssh_called.update(value=True) or {"success": True},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push", "--source", "remote"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "has no internet" in result.output
    assert ssh_called["value"] is False


def test_sync_source_bundle_forces_bundle_even_on_internet_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    bundle_called = {"value": False}
    bundle_kwargs: Dict[str, Any] = {}
    ssh_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh_bundle",
        lambda *args, **kwargs: bundle_called.update(value=True)
        or bundle_kwargs.update(kwargs)
        or {"success": True, "synced_sha": "a" * 40, "error": None},
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh",
        lambda *args, **kwargs: ssh_called.update(value=True)
        or {"success": False, "synced_sha": None, "error": "should not run"},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push", "--source", "bundle"])

    assert result.exit_code == EXIT_SUCCESS
    assert bundle_called["value"] is True
    assert ssh_called["value"] is False
    assert bundle_kwargs["force"] is False


def test_sync_source_bundle_force_passes_hard_reset_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    bundle_kwargs: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh_bundle",
        lambda *args, **kwargs: bundle_kwargs.update(kwargs)
        or {"success": True, "synced_sha": "a" * 40, "error": None},
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh",
        lambda *args, **kwargs: {"success": False, "synced_sha": None, "error": "should not run"},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push", "--source", "bundle", "--force"])

    assert result.exit_code == EXIT_SUCCESS
    assert bundle_kwargs["force"] is True


def test_sync_resolves_commit_and_message_from_current_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}
    called = {"sha_revision": "", "msg_revision": ""}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(sync_cmd_module, "get_current_branch", lambda: "main")
    monkeypatch.setattr(
        sync_cmd_module,
        "get_current_commit_sha",
        lambda revision="HEAD": called.__setitem__("sha_revision", revision) or "a" * 40,
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "get_commit_message",
        lambda revision="HEAD": called.__setitem__("msg_revision", revision) or "feature commit",
    )
    monkeypatch.setattr(sync_cmd_module, "has_uncommitted_changes", lambda: False)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    def fake_sync_via_ssh(*args: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"success": True, "synced_sha": "a" * 40, "error": None}

    monkeypatch.setattr(sync_cmd_module, "sync_via_ssh", fake_sync_via_ssh)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push"])

    assert result.exit_code == EXIT_SUCCESS
    assert called["sha_revision"] == "main"
    assert called["msg_revision"] == "main"
    assert captured["commit_sha"] == "a" * 40


def test_sync_rejects_removed_branch_option() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--branch", "feature/test"])

    assert result.exit_code != EXIT_SUCCESS
    assert "No such option: --branch" in result.output


def test_sync_default_bundle_mode_uses_best_effort_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(
        sync_cmd_module, "load_tunnel_config", make_gpu_only_no_internet_tunnel_config
    )
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sync_cmd_module.click.ClickException("push failed")
        ),
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh_bundle",
        lambda *args, **kwargs: {"success": True, "synced_sha": "a" * 40, "error": None},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync"])

    assert result.exit_code == EXIT_SUCCESS
    assert "best-effort" in result.output


def test_sync_default_remote_mode_requires_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    ssh_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sync_cmd_module.click.ClickException("push failed")
        ),
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh",
        lambda *args, **kwargs: ssh_called.update(value=True)
        or {"success": True, "synced_sha": "a" * 40, "error": None},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "push failed" in result.output
    assert ssh_called["value"] is False


def test_sync_push_mode_best_effort_continues_on_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    ssh_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sync_cmd_module.click.ClickException("push failed")
        ),
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh",
        lambda *args, **kwargs: ssh_called.update(value=True)
        or {"success": True, "synced_sha": "a" * 40, "error": None},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--source", "remote", "--push-mode", "best-effort"])

    assert result.exit_code == EXIT_SUCCESS
    assert "best-effort" in result.output
    assert ssh_called["value"] is True


def test_sync_force_defaults_to_best_effort_push_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    ssh_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sync_cmd_module.click.ClickException("push failed")
        ),
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh",
        lambda *args, **kwargs: ssh_called.update(value=True)
        or {"success": True, "synced_sha": "a" * 40, "error": None},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--source", "remote", "--force"])

    assert result.exit_code == EXIT_SUCCESS
    assert "best-effort" in result.output
    assert ssh_called["value"] is True


def test_sync_force_respects_explicit_push_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    ssh_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sync_cmd_module.click.ClickException("push failed")
        ),
    )
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh",
        lambda *args, **kwargs: ssh_called.update(value=True)
        or {"success": True, "synced_sha": "a" * 40, "error": None},
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["sync", "--source", "remote", "--force", "--push-mode", "required"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "push failed" in result.output
    assert ssh_called["value"] is False


def test_sync_no_push_conflicts_with_non_skip_push_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(sync_cmd_module, "get_current_branch", lambda: "main")

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push", "--push-mode", "required"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "conflicts" in result.output


def test_sync_fails_on_dirty_tree_without_allow_dirty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    push_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(sync_cmd_module, "get_current_branch", lambda: "main")
    monkeypatch.setattr(sync_cmd_module, "has_uncommitted_changes", lambda: True)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "push_to_remote",
        lambda *args, **kwargs: push_called.update(value=True),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert push_called["value"] is False
    assert "Uncommitted changes detected" in result.output
    assert "--allow-dirty" in result.output
    assert "Use --force only" in result.output


def test_sync_allow_dirty_continues_with_committed_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(sync_cmd_module, "get_current_branch", lambda: "main")
    monkeypatch.setattr(sync_cmd_module, "get_current_commit_sha", lambda revision="HEAD": "a" * 40)
    monkeypatch.setattr(
        sync_cmd_module, "get_commit_message", lambda revision="HEAD": "test commit"
    )
    monkeypatch.setattr(sync_cmd_module, "has_uncommitted_changes", lambda: True)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    def fake_sync_via_ssh(*args: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"success": True, "synced_sha": "a" * 40, "error": None}

    monkeypatch.setattr(sync_cmd_module, "sync_via_ssh", fake_sync_via_ssh)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push", "--allow-dirty"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["commit_sha"] == "a" * 40
    assert "syncing committed tip of 'main' only" in result.output


def test_sync_force_allows_dirty_tree_with_committed_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(sync_cmd_module, "get_current_branch", lambda: "main")
    monkeypatch.setattr(sync_cmd_module, "get_current_commit_sha", lambda revision="HEAD": "a" * 40)
    monkeypatch.setattr(
        sync_cmd_module, "get_commit_message", lambda revision="HEAD": "test commit"
    )
    monkeypatch.setattr(sync_cmd_module, "has_uncommitted_changes", lambda: True)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    def fake_sync_via_ssh(*args: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"success": True, "synced_sha": "a" * 40, "error": None}

    monkeypatch.setattr(sync_cmd_module, "sync_via_ssh", fake_sync_via_ssh)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push", "--force"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["commit_sha"] == "a" * 40
    assert captured["force"] is True
    assert "syncing committed tip of 'main' only (--force)" in result.output


def test_sync_failure_summarizes_divergence_and_filters_locale_noise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(
        sync_cmd_module, "load_tunnel_config", make_gpu_only_no_internet_tunnel_config
    )
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    raw_error = """
bash: warning: setlocale: LC_ALL: cannot change locale (en_US.UTF-8)
From /tmp/inspire-sync-lcfhcmjl.bundle
 * branch            deadbeef -> FETCH_HEAD
Already on 'main'
hint: Diverging branches can't be fast-forwarded, you need to either:
fatal: Not possible to fast-forward, aborting.
""".strip()

    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh_bundle",
        lambda *args, **kwargs: {"success": False, "synced_sha": None, "error": raw_error},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["sync", "--no-push", "--source", "bundle"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert (
        "Sync failed: Branch 'main' on Bridge diverged and cannot be fast-forwarded."
        in result.output
    )
    assert "Hint: Reconcile branch history (merge/rebase) and retry sync." in result.output
    assert "setlocale" not in result.output
    assert "Diverging branches can't be fast-forwarded" not in result.output


def test_sync_failure_shows_raw_details_only_in_debug_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(sync_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    raw_error = "fatal: example sync failure\nextra detail line"
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh",
        lambda *args, **kwargs: {"success": False, "synced_sha": None, "error": raw_error},
    )

    runner = CliRunner()
    normal = runner.invoke(cli_main, ["sync", "--no-push", "--source", "remote"])
    debug = runner.invoke(cli_main, ["--debug", "sync", "--no-push", "--source", "remote"])

    assert normal.exit_code == EXIT_GENERAL_ERROR
    assert "Sync failed: fatal: example sync failure" in normal.output
    assert "extra detail line" not in normal.output

    assert debug.exit_code == EXIT_GENERAL_ERROR
    assert "Sync failed: fatal: example sync failure" in debug.output
    assert "Details:" in debug.output
    assert "extra detail line" in debug.output


def test_sync_failure_json_output_uses_summarized_message_and_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    _patch_common_git_helpers(monkeypatch)
    monkeypatch.setattr(
        sync_cmd_module, "load_tunnel_config", make_gpu_only_no_internet_tunnel_config
    )
    monkeypatch.setattr(sync_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sync_cmd_module,
        "sync_via_ssh_bundle",
        lambda *args, **kwargs: {
            "success": False,
            "synced_sha": None,
            "error": "bash: warning: setlocale: LC_ALL: cannot change locale (en_US.UTF-8)\n"
            "fatal: Not possible to fast-forward, aborting.",
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "sync", "--no-push", "--source", "bundle"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert (
        payload["error"]["message"]
        == "Branch 'main' on Bridge diverged and cannot be fast-forwarded."
    )
    assert "Reconcile branch history" in payload["error"]["hint"]
    assert "setlocale" not in payload["error"]["message"]
