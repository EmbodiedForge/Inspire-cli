import importlib
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
    monkeypatch.setattr(sync_cmd_module, "get_current_commit_sha", lambda: "a" * 40)
    monkeypatch.setattr(sync_cmd_module, "get_commit_message", lambda: "test commit")
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
    monkeypatch.setattr(sync_cmd_module, "get_current_commit_sha", lambda: "a" * 40)
    monkeypatch.setattr(sync_cmd_module, "get_commit_message", lambda: "test commit")
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
    assert "syncing committed HEAD only" in result.output
