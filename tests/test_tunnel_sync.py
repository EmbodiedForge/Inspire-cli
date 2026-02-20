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
    call_count = {"run_ssh_command": 0}

    def fake_subprocess_run(args: list[str], *unused: Any, **kwargs: Any) -> FakeCompletedProcess:
        captured["bundle_args"] = args
        assert kwargs.get("check") is True
        return FakeCompletedProcess(returncode=0, stdout="", stderr="")

    def fake_run_scp_transfer(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        captured["scp_kwargs"] = kwargs
        captured["scp_local_path"] = kwargs["local_path"]
        return FakeCompletedProcess(returncode=0)

    def fake_run_ssh_command(command: str, *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        call_count["run_ssh_command"] += 1
        if call_count["run_ssh_command"] == 1:
            # probe command: no existing branch tip on remote
            return FakeCompletedProcess(returncode=0, stdout="")
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


def test_sync_via_ssh_bundle_uses_incremental_range(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    commit_sha = "c" * 40
    base_sha = "b" * 40
    call_count = {"run_ssh_command": 0}

    def fake_subprocess_run(args: list[str], *unused: Any, **kwargs: Any) -> FakeCompletedProcess:
        if args[:3] == ["git", "cat-file", "-e"]:
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        if args[:3] == ["git", "rev-list", "--count"]:
            return FakeCompletedProcess(returncode=0, stdout="2\n", stderr="")
        if args[:3] == ["git", "bundle", "create"]:
            captured["bundle_args"] = args
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        raise AssertionError(f"Unexpected git call: {args}")

    def fake_run_scp_transfer(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        return FakeCompletedProcess(returncode=0)

    def fake_run_ssh_command(command: str, *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        call_count["run_ssh_command"] += 1
        if call_count["run_ssh_command"] == 1:
            # probe command
            return FakeCompletedProcess(returncode=0, stdout=f"{base_sha}\n")
        return FakeCompletedProcess(returncode=0, stdout=f"{commit_sha}\n")

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
    assert result["bundle_mode"] == "incremental"
    assert result["bundle_base_sha"] == base_sha
    assert captured["bundle_args"][-1] == f"{base_sha}..{commit_sha}"


def test_sync_via_ssh_bundle_treats_empty_incremental_range_as_up_to_date(monkeypatch) -> None:
    commit_sha = "e" * 40
    base_sha = "f" * 40
    called = {"scp": False, "bundle_create": False}

    def fake_subprocess_run(args: list[str], *unused: Any, **kwargs: Any) -> FakeCompletedProcess:
        if args[:3] == ["git", "cat-file", "-e"]:
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        if args[:3] == ["git", "rev-list", "--count"]:
            return FakeCompletedProcess(returncode=0, stdout="0\n", stderr="")
        if args[:3] == ["git", "bundle", "create"]:
            called["bundle_create"] = True
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        raise AssertionError(f"Unexpected git call: {args}")

    def fake_run_scp_transfer(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        called["scp"] = True
        return FakeCompletedProcess(returncode=0)

    def fake_run_ssh_command(command: str, *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        return FakeCompletedProcess(returncode=0, stdout=f"{base_sha}\n")

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
    assert result["bundle_mode"] == "up_to_date"
    assert result["bundle_base_sha"] == base_sha
    assert called["bundle_create"] is False
    assert called["scp"] is False


def test_sync_via_ssh_bundle_falls_back_to_full_when_incremental_create_fails(monkeypatch) -> None:
    captured: dict[str, Any] = {"bundle_revs": []}
    commit_sha = "1" * 40
    base_sha = "2" * 40
    call_count = {"run_ssh_command": 0}

    def fake_subprocess_run(args: list[str], *unused: Any, **kwargs: Any) -> FakeCompletedProcess:
        if args[:3] == ["git", "cat-file", "-e"]:
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        if args[:3] == ["git", "rev-list", "--count"]:
            return FakeCompletedProcess(returncode=0, stdout="3\n", stderr="")
        if args[:3] == ["git", "bundle", "create"]:
            captured["bundle_revs"].append(args[-1])
            if args[-1] != "HEAD":
                raise sync_module.subprocess.CalledProcessError(
                    1, args, stderr="incremental failed"
                )
            return FakeCompletedProcess(returncode=0, stdout="", stderr="")
        raise AssertionError(f"Unexpected git call: {args}")

    def fake_run_scp_transfer(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        return FakeCompletedProcess(returncode=0)

    def fake_run_ssh_command(command: str, *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        call_count["run_ssh_command"] += 1
        if call_count["run_ssh_command"] == 1:
            return FakeCompletedProcess(returncode=0, stdout=f"{base_sha}\n")
        return FakeCompletedProcess(returncode=0, stdout=f"{commit_sha}\n")

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
    assert result["bundle_mode"] == "full"
    assert captured["bundle_revs"] == [f"{base_sha}..{commit_sha}", "HEAD"]


def test_sync_via_ssh_bundle_returns_fast_when_up_to_date(monkeypatch) -> None:
    commit_sha = "d" * 40
    called = {"scp": False, "git": False}

    def fake_subprocess_run(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        called["git"] = True
        return FakeCompletedProcess(returncode=0, stdout="", stderr="")

    def fake_run_scp_transfer(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        called["scp"] = True
        return FakeCompletedProcess(returncode=0)

    def fake_run_ssh_command(command: str, *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        # probe command sees target already at commit_sha
        return FakeCompletedProcess(returncode=0, stdout=f"{commit_sha}\n")

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
    assert result["bundle_mode"] == "up_to_date"
    assert called["scp"] is False
    assert called["git"] is False
