import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
import importlib

import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main
from inspire.cli.context import EXIT_GENERAL_ERROR, EXIT_SUCCESS, EXIT_TIMEOUT
from inspire.cli.utils.config import Config

# Import the module itself, not the click group
bridge_module = importlib.import_module("inspire.cli.commands.bridge")


def make_sync_config(tmp_path: Path) -> Config:
    return Config(
        username="",
        password="",
        target_dir=str(tmp_path),
        gitea_repo="owner/repo",
        gitea_token="token",
        gitea_server="https://gitea.example.com",
        default_remote="origin",
        remote_timeout=5,
        bridge_action_timeout=5,
        bridge_action_denylist=[],
    )


def test_bridge_exec_triggers_and_no_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)

    called: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(
        config: Config,
        raw_command: str,
        artifact_paths: List[str],
        request_id: str,
        denylist: Optional[List[str]] = None,
    ) -> None:
        called["trigger"] = {
            "raw_command": raw_command,
            "artifact_paths": artifact_paths,
            "request_id": request_id,
            "denylist": denylist,
        }

    monkeypatch.setattr(bridge_module, "trigger_bridge_action_workflow", fake_trigger)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["bridge", "exec", "echo hi", "--no-wait", "--no-tunnel"])

    assert result.exit_code == EXIT_SUCCESS
    assert "trigger" in called
    assert called["trigger"]["raw_command"] == "echo hi"


def test_bridge_exec_uses_env_denylist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)
    config.bridge_action_denylist = ["rm -rf /"]

    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(
        config: Config,
        raw_command: str,
        artifact_paths: List[str],
        request_id: str,
        denylist: Optional[List[str]] = None,
    ) -> None:
        captured["denylist"] = denylist

    monkeypatch.setattr(bridge_module, "trigger_bridge_action_workflow", fake_trigger)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["bridge", "exec", "echo hi", "--no-wait", "--no-tunnel"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["denylist"] == ["rm -rf /"]


def test_bridge_exec_reports_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_wait(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "completed", "conclusion": "failure", "html_url": "http://example.com"}

    def fake_fetch_log(*args: Any, **kwargs: Any) -> Optional[str]:
        return None

    monkeypatch.setattr(bridge_module, "trigger_bridge_action_workflow", fake_trigger)
    monkeypatch.setattr(bridge_module, "wait_for_bridge_action_completion", fake_wait)
    monkeypatch.setattr(bridge_module, "fetch_bridge_output_log", fake_fetch_log)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["bridge", "exec", "echo hi", "--no-tunnel"])

    assert result.exit_code == EXIT_GENERAL_ERROR


def test_bridge_exec_displays_output_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that command output is displayed to the user."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_wait(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "completed", "conclusion": "success", "html_url": "http://example.com"}

    def fake_fetch_log(*args: Any, **kwargs: Any) -> Optional[str]:
        return "Hello from Bridge!\nCommand completed."

    monkeypatch.setattr(bridge_module, "trigger_bridge_action_workflow", fake_trigger)
    monkeypatch.setattr(bridge_module, "wait_for_bridge_action_completion", fake_wait)
    monkeypatch.setattr(bridge_module, "fetch_bridge_output_log", fake_fetch_log)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["bridge", "exec", "echo hi", "--no-tunnel"])

    assert result.exit_code == EXIT_SUCCESS
    assert "--- Command Output ---" in result.output
    assert "Hello from Bridge!" in result.output
    assert "Command completed." in result.output
    assert "--- End Output ---" in result.output


def test_bridge_exec_json_includes_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that JSON output includes the command output."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_wait(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "completed", "conclusion": "success", "html_url": "http://example.com"}

    def fake_fetch_log(*args: Any, **kwargs: Any) -> Optional[str]:
        return "Test output"

    monkeypatch.setattr(bridge_module, "trigger_bridge_action_workflow", fake_trigger)
    monkeypatch.setattr(bridge_module, "wait_for_bridge_action_completion", fake_wait)
    monkeypatch.setattr(bridge_module, "fetch_bridge_output_log", fake_fetch_log)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "bridge", "exec", "echo hi", "--no-tunnel"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["status"] == "success"
    assert payload["data"]["output"] == "Test output"


# Tests for SSH tunnel streaming functionality


def test_bridge_exec_ssh_streaming_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that SSH tunnel uses streaming for human output."""
    config = make_sync_config(tmp_path)
    streamed_lines: List[str] = []

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return True

    def fake_run_ssh_command_streaming(
        command: str,
        bridge_name: Any = None,
        config: Any = None,
        timeout: Any = None,
        output_callback: Any = None,
    ) -> int:
        # Simulate streaming output
        lines = ["Line 1\n", "Line 2\n", "Line 3\n"]
        for line in lines:
            streamed_lines.append(line)
            if output_callback:
                output_callback(line)
        return 0

    monkeypatch.setattr(bridge_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(bridge_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["bridge", "exec", "echo test"])

    assert result.exit_code == EXIT_SUCCESS
    assert "Using SSH tunnel (fast path)" in result.output
    assert "--- Command Output ---" in result.output
    assert "--- End Output ---" in result.output
    assert "OK Command completed successfully (via SSH)" in result.output
    # Verify streaming function was called (output was streamed)
    assert len(streamed_lines) == 3


def test_bridge_exec_ssh_json_uses_buffered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that JSON mode uses buffered output, not streaming."""
    config = make_sync_config(tmp_path)
    streaming_called = {"value": False}
    buffered_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return True

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        streaming_called["value"] = True
        return 0

    class FakeCompletedProcess:
        returncode = 0
        stdout = "buffered output"
        stderr = ""

    def fake_run_ssh_command(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        buffered_called["value"] = True
        return FakeCompletedProcess()

    monkeypatch.setattr(bridge_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(bridge_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming)
    monkeypatch.setattr(bridge_module, "run_ssh_command", fake_run_ssh_command)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "bridge", "exec", "echo test"])

    assert result.exit_code == EXIT_SUCCESS
    # Buffered should be used, not streaming
    assert buffered_called["value"] is True
    assert streaming_called["value"] is False
    # Verify JSON output
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["method"] == "ssh_tunnel"
    assert payload["data"]["output"] == "buffered output"


def test_bridge_exec_ssh_streaming_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that streaming mode handles timeout correctly."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return True

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

    monkeypatch.setattr(bridge_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(bridge_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["bridge", "exec", "sleep 100", "--timeout", "5"])

    assert result.exit_code == EXIT_TIMEOUT
    assert "timed out" in result.output.lower()


def test_bridge_exec_ssh_streaming_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that streaming mode handles command failure correctly."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return True

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        return 1  # Non-zero exit code

    monkeypatch.setattr(bridge_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(bridge_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["bridge", "exec", "false"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Command failed with exit code 1" in result.output
