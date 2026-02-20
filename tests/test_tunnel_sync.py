from typing import Any

from inspire.bridge.tunnel import sync as sync_module


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_sync_via_ssh_uses_remote_and_commit(monkeypatch) -> None:
    captured = {"command": ""}
    commit_sha = "a" * 40

    def fake_run_ssh_command(command: str, *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        captured["command"] = command
        return FakeCompletedProcess(returncode=0, stdout=f"info\n{commit_sha}\n")

    monkeypatch.setattr(sync_module, "run_ssh_command", fake_run_ssh_command)

    result = sync_module.sync_via_ssh(
        target_dir="/remote/project",
        branch="main",
        commit_sha=commit_sha,
        remote="upstream",
    )

    assert result["success"] is True
    assert "git fetch upstream main" in captured["command"]
    assert f"git merge --ff-only {commit_sha}" in captured["command"]
    assert "expected_sha=" in captured["command"]


def test_sync_via_ssh_force_uses_hard_reset(monkeypatch) -> None:
    captured = {"command": ""}

    def fake_run_ssh_command(command: str, *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        captured["command"] = command
        return FakeCompletedProcess(returncode=0, stdout="ok\n")

    monkeypatch.setattr(sync_module, "run_ssh_command", fake_run_ssh_command)

    sync_module.sync_via_ssh(
        target_dir="/remote/project",
        branch="main",
        commit_sha="b" * 40,
        remote="origin",
        force=True,
    )

    assert "git reset --hard" in captured["command"]


def test_sync_via_ssh_bundle_uses_scp_and_remote_fetch(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    commit_sha = "c" * 40

    def fake_subprocess_run(args: list[str], *unused: Any, **kwargs: Any) -> FakeCompletedProcess:
        captured["bundle_args"] = args
        assert kwargs.get("check") is True
        return FakeCompletedProcess(returncode=0, stdout="", stderr="")

    def fake_run_scp_transfer(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        captured["scp_kwargs"] = kwargs
        captured["scp_local_path"] = kwargs["local_path"]
        return FakeCompletedProcess(returncode=0)

    def fake_run_ssh_command(command: str, *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        captured["remote_command"] = command
        captured["ssh_kwargs"] = kwargs
        return FakeCompletedProcess(returncode=0, stdout=f"done\n{commit_sha}\n")

    monkeypatch.setattr(sync_module.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(sync_module, "run_scp_transfer", fake_run_scp_transfer)
    monkeypatch.setattr(sync_module, "run_ssh_command", fake_run_ssh_command)

    result = sync_module.sync_via_ssh_bundle(
        target_dir="/remote/project",
        branch="main",
        commit_sha=commit_sha,
        bridge_name="gpu-offline",
    )

    assert result["success"] is True
    assert result["synced_sha"] == commit_sha
    assert captured["bundle_args"][:3] == ["git", "bundle", "create"]
    assert captured["bundle_args"][-1] == "HEAD"
    assert captured["scp_kwargs"]["bridge_name"] == "gpu-offline"
    assert "git fetch" in captured["remote_command"]
    assert commit_sha in captured["remote_command"]
