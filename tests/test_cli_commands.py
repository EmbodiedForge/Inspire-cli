import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main
from inspire.cli.context import (
    EXIT_SUCCESS,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_API_ERROR,
    EXIT_TIMEOUT,
    EXIT_LOG_NOT_FOUND,
    EXIT_JOB_NOT_FOUND,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.utils import config as config_module
from inspire.cli.utils import auth as auth_module
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.config import ConfigError
from inspire.cli.utils.job_cache import JobCache
from inspire.inspire_api_control import ResourceManager

# Valid test job IDs (must match the format: job-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
TEST_JOB_ID = "job-12345678-1234-1234-1234-123456789abc"
TEST_JOB_ID_2 = "job-abcdef12-3456-7890-abcd-ef1234567890"
TEST_JOB_ID_3 = "job-11111111-2222-3333-4444-555555555555"


def make_test_config(tmp_path: Path, include_compute_groups: bool = False) -> config_module.Config:
    """Create a test Config object.

    Args:
        tmp_path: Temporary directory path
        include_compute_groups: If True, include test compute groups
    """
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )
    # Add test compute groups if requested
    if include_compute_groups:
        test_group_id = "lcg-test000-0000-0000-0000-000000000000"
        config.compute_groups = [
            {
                "name": "H200 TestRoom",
                "id": test_group_id,
                "gpu_type": "H200",
                "location": "Test",
            }
        ]
    return config


class DummyAPI:
    def __init__(self) -> None:
        self.calls: Dict[str, Any] = {}
        self.resource_manager = ResourceManager()

    # Job-related methods -------------------------------------------------
    def create_training_job_smart(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls["create_training_job_smart"] = kwargs
        return {"data": {"job_id": TEST_JOB_ID}}

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        self.calls.setdefault("get_job_detail", []).append(job_id)
        return {
            "data": {
                "job_id": job_id,
                "name": "test-job",
                "status": "SUCCEEDED",
                "running_time_ms": "1000",
            }
        }

    def stop_training_job(self, job_id: str) -> None:
        self.calls.setdefault("stop_training_job", []).append(job_id)

    # Resource / nodes ----------------------------------------------------
    def list_cluster_nodes(
        self,
        page_num: int,
        page_size: int,
        resource_pool: Optional[str],
    ) -> Dict[str, Any]:
        self.calls["list_cluster_nodes"] = {
            "page_num": page_num,
            "page_size": page_size,
            "resource_pool": resource_pool,
        }
        return {
            "data": {
                "nodes": [
                    {
                        "node_id": "node-1",
                        "resource_pool": resource_pool or "online",
                        "status": "ready",
                        "gpu_count": 4,
                    }
                ],
                "total": 1,
            }
        }


def patch_config_and_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, include_compute_groups: bool = False) -> DummyAPI:
    """Patch Config.from_env and AuthManager.get_api to use local stubs.

    Args:
        monkeypatch: pytest monkeypatch fixture
        tmp_path: Temporary directory path
        include_compute_groups: If True, include test compute groups in config
    """
    config = make_test_config(tmp_path, include_compute_groups=include_compute_groups)
    config.target_dir and Path(config.target_dir).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INSPIRE_JOB_CACHE", config.job_cache_path)

    def fake_from_env(cls, require_target_dir: bool = False) -> config_module.Config:  # type: ignore[override]
        if require_target_dir and not config.target_dir:
            raise ConfigError("Missing INSPIRE_TARGET_DIR")
        return config

    def fake_from_files_and_env(cls, require_target_dir: bool = False, require_credentials: bool = True) -> tuple:  # type: ignore[override]
        if require_target_dir and not config.target_dir:
            raise ConfigError("Missing INSPIRE_TARGET_DIR")
        return config, {}

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))
    monkeypatch.setattr(config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env))

    api = DummyAPI()

    def fake_get_api(self_or_cls, cfg: Optional[config_module.Config] = None) -> DummyAPI:  # type: ignore[override]
        # Ensure we were passed the same config object
        assert cfg is config or cfg is None
        return api

    monkeypatch.setattr(auth_module.AuthManager, "get_api", fake_get_api)
    auth_module.AuthManager.clear_cache()

    return api


# ---------------------------------------------------------------------------
# Global main entry with subcommands
# ---------------------------------------------------------------------------


def test_global_json_flag_with_resources_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Include test compute groups in config
    patch_config_and_auth(monkeypatch, tmp_path, include_compute_groups=True)
    from inspire.cli.utils import browser_api as browser_api_module

    # Use a test placeholder UUID instead of real compute group ID
    test_group_id = "lcg-test000-0000-0000-0000-000000000000"
    monkeypatch.setattr(
        browser_api_module,
        "get_accurate_gpu_availability",
        lambda: [
            browser_api_module.GPUAvailability(
                group_id=test_group_id,
                group_name="H200 TestRoom",
                gpu_type="NVIDIA H200",
                total_gpus=128,
                used_gpus=32,
                available_gpus=96,
                low_priority_gpus=8,
            )
        ],
    )
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "resources", "list"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["success"] is True
    assert "availability" in payload["data"]
    assert payload["data"]["availability"][0]["group_id"] == test_group_id


def test_global_debug_flag_runs_subcommand(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.utils import browser_api as browser_api_module

    monkeypatch.setattr(
        browser_api_module,
        "get_accurate_gpu_availability",
        lambda: [],
    )
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--debug", "resources", "list"])
    assert result.exit_code == 0


def test_job_help_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Smoke test to ensure `inspire job --help` works (no import/syntax errors)."""
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["job", "--help"])
    assert result.exit_code == 0
    assert "Manage training jobs" in result.output


# ---------------------------------------------------------------------------
# Job command group
# ---------------------------------------------------------------------------


def test_job_create_human_output_updates_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "job",
            "create",
            "--name",
            "test-job",
            "--resource",
            "H200",
            "--command",
            "echo hi",
            "--no-auto",
        ],
    )

    assert result.exit_code == 0
    assert "Job created: job-123" in result.output

    # Verify job cache file was created
    cache_path = Path(make_test_config(tmp_path).job_cache_path)
    assert cache_path.exists()


def test_job_create_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "test-job",
            "--resource",
            "H200",
            "--command",
            "echo hi",
            "--no-auto",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["data"]["job_id"] == TEST_JOB_ID


def test_job_create_requires_target_dir(monkeypatch: pytest.MonkeyPatch):
    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):
        assert require_target_dir is True
        raise ConfigError("Missing INSPIRE_TARGET_DIR")

    monkeypatch.setattr(config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env))

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "job",
            "create",
            "--name",
            "test-job",
            "--resource",
            "H200",
            "--command",
            "echo hi",
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "Missing INSPIRE_TARGET_DIR" in result.output


def test_wrap_in_bash():
    """Test the bash wrapper helper function."""
    from inspire.cli.commands.job import _wrap_in_bash

    # Basic wrapping
    assert _wrap_in_bash("python train.py") == "bash -c 'python train.py'"

    # Source command (the main use case)
    result = _wrap_in_bash("source .env && python train.py")
    assert result == "bash -c 'source .env && python train.py'"

    # Escape single quotes
    result = _wrap_in_bash("echo 'hello'")
    assert result == "bash -c 'echo '\\''hello'\\'''"

    # Skip if already wrapped
    assert _wrap_in_bash("bash -c 'foo'") == "bash -c 'foo'"
    assert _wrap_in_bash("sh -c 'foo'") == "sh -c 'foo'"
    assert _wrap_in_bash("/bin/bash -c 'foo'") == "/bin/bash -c 'foo'"
    assert _wrap_in_bash("/bin/sh -c 'foo'") == "/bin/sh -c 'foo'"

    # Whitespace handling
    assert _wrap_in_bash("  bash -c 'foo'  ") == "  bash -c 'foo'  "


def test_job_status_updates_cache_and_formats(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["job", "status", TEST_JOB_ID])
    assert result.exit_code == 0
    assert "Job Status" in result.output
    assert TEST_JOB_ID in result.output


def test_job_command_prefers_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    # Seed cache with a different command to ensure API is preferred
    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="cached command",
        status="RUNNING",
        log_path=None,
    )

    def api_detail(job_id: str) -> Dict[str, Any]:
        api.calls.setdefault("get_job_detail", []).append(job_id)
        return {"data": {"job_id": job_id, "command": "api command"}}

    api.get_job_detail = api_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "command", TEST_JOB_ID])

    assert result.exit_code == 0
    assert "api command" in result.output
    assert "cached command" not in result.output
    assert api.calls["get_job_detail"] == [TEST_JOB_ID]


def test_job_command_falls_back_to_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="cached command",
        status="RUNNING",
        log_path=None,
    )

    def api_detail(job_id: str) -> Dict[str, Any]:  # noqa: ARG001
        raise AuthenticationError("bad credentials")

    api.get_job_detail = api_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "command", TEST_JOB_ID])

    assert result.exit_code == 0
    assert "cached command" in result.output


def test_job_status_not_found_sets_specific_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    def failing_get_job_detail(job_id: str) -> Dict[str, Any]:
        raise RuntimeError("Job not found")

    api.get_job_detail = failing_get_job_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "status", "missing-id"])
    assert result.exit_code == EXIT_JOB_NOT_FOUND


def test_job_stop_with_force_and_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        ["--json", "job", "stop", TEST_JOB_ID],
    )
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert data["data"]["job_id"] == TEST_JOB_ID
    assert data["data"]["status"] == "stopped"


def test_job_wait_succeeds_and_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    # Ensure the job is immediately in a terminal state
    def get_job_detail(job_id: str) -> Dict[str, Any]:
        return {
            "data": {
                "job_id": job_id,
                "name": "wait-job",
                "status": "SUCCEEDED",
                "running_time_ms": "1000",
            }
        }

    api.get_job_detail = get_job_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["job", "wait", TEST_JOB_ID, "--timeout", "60", "--interval", "1"],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "SUCCEEDED" in result.output


def test_job_wait_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Force time to jump ahead so we immediately hit timeout
    from importlib import import_module

    job_cmd = import_module("inspire.cli.commands.job")

    calls: List[int] = []

    def fake_time() -> int:
        # First call (start_time) -> 0, second call -> large value
        calls.append(1)
        return 0 if len(calls) == 1 else 10

    monkeypatch.setattr(job_cmd.time, "time", fake_time)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["job", "wait", TEST_JOB_ID, "--timeout", "1", "--interval", "1"],
    )
    assert result.exit_code == EXIT_TIMEOUT
    assert "Timeout after 1s" in result.output


def test_job_list_uses_local_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Provide a fake JobCache implementation
    from importlib import import_module

    job_cmd = import_module("inspire.cli.commands.job")

    class FakeCache:
        def __init__(self, path: str) -> None:  # noqa: ARG002
            pass

        def list_jobs(self, limit: int = 10, status: Optional[str] = None, exclude_statuses: Optional[set] = None) -> List[Dict[str, Any]]:
            return [
                {
                    "job_id": TEST_JOB_ID,
                    "name": "cached-job",
                    "status": status or "PENDING",
                    "created_at": "2025-01-01T00:00:00",
                }
            ]

    monkeypatch.setattr(job_cmd, "JobCache", FakeCache)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "list", "--limit", "5"])

    assert result.exit_code == 0
    assert "cached-job" in result.output
    assert TEST_JOB_ID in result.output


def test_job_update_refreshes_job_creating_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    # Seed cache with a job in an early-stage API status that should still be refreshed
    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="creating-job",
        resource="H200",
        command="echo hi",
        status="job_creating",
        log_path=None,
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "job", "update", "--delay", "0"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True

    updated_ids = {entry["job_id"] for entry in payload["data"]["updated"]}
    assert updated_ids == {TEST_JOB_ID}

    # Ensure the job was actually polled and the cache was updated
    assert api.calls["get_job_detail"] == [TEST_JOB_ID]
    refreshed = cache.get_job(TEST_JOB_ID)
    assert refreshed is not None
    assert refreshed["status"] == "SUCCEEDED"


def test_job_logs_path_and_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache with a remote log path
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache

    cache = JobCache(config.get_expanded_cache_path())
    remote_log_path = f"/train/logs/.inspire/training_master_{TEST_JOB_ID}.log"
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=remote_log_path,
    )

    # Create local cache directory and log file (simulating already-fetched log)
    local_cache_dir = Path(config.log_cache_dir)
    local_cache_dir.mkdir(parents=True, exist_ok=True)
    local_log_path = local_cache_dir / f"{TEST_JOB_ID}.log"
    local_log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")

    # Mock fetch_remote_log_via_bridge to do nothing (log already cached)
    from importlib import import_module

    job_cmd = import_module("inspire.cli.commands.job")

    def fake_fetch(config, job_id, remote_log_path, cache_path, refresh):  # noqa: ARG001
        pass  # Log already exists locally

    monkeypatch.setattr(job_cmd, "fetch_remote_log_via_bridge", fake_fetch)

    runner = CliRunner()

    # --path just prints path
    result = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID, "--path"])
    assert result.exit_code == 0
    assert str(remote_log_path) in result.output

    # --tail reads last N lines
    result_tail = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID, "--tail", "2"])
    assert result_tail.exit_code == 0
    assert "line2" in result_tail.output
    assert "line3" in result_tail.output


def test_job_logs_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache with a remote log path
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache

    cache = JobCache(config.get_expanded_cache_path())
    remote_log_path = f"/train/logs/.inspire/training_master_{TEST_JOB_ID}.log"
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=remote_log_path,
    )

    # Create local cache directory and log file
    local_cache_dir = Path(config.log_cache_dir)
    local_cache_dir.mkdir(parents=True, exist_ok=True)
    local_log_path = local_cache_dir / f"{TEST_JOB_ID}.log"
    local_log_path.write_text("test log content\n", encoding="utf-8")

    # Mock fetch_remote_log_via_bridge
    from importlib import import_module

    job_cmd = import_module("inspire.cli.commands.job")

    def fake_fetch(config, job_id, remote_log_path, cache_path, refresh):  # noqa: ARG001
        pass

    monkeypatch.setattr(job_cmd, "fetch_remote_log_via_bridge", fake_fetch)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "job", "logs", TEST_JOB_ID])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert "log_path" in data["data"]
    assert "content" in data["data"]
    assert "test log content" in data["data"]["content"]


def test_job_logs_legacy_filename_is_migrated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache

    cache = JobCache(config.get_expanded_cache_path())
    remote_log_path = f"/train/logs/.inspire/training_master_{TEST_JOB_ID}.log"
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=remote_log_path,
    )

    local_cache_dir = Path(config.log_cache_dir)
    local_cache_dir.mkdir(parents=True, exist_ok=True)
    legacy_log_path = local_cache_dir / f"job-{TEST_JOB_ID}.log"
    legacy_log_path.write_text("legacy line1\nlegacy line2\n", encoding="utf-8")

    from importlib import import_module

    job_cmd = import_module("inspire.cli.commands.job")

    def fail_fetch(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("fetch should not be called when legacy cache exists")

    monkeypatch.setattr(job_cmd, "fetch_remote_log_via_bridge", fail_fetch)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID, "--tail", "1"])

    assert result.exit_code == 0
    assert "legacy line2" in result.output
    new_path = local_cache_dir / f"{TEST_JOB_ID}.log"
    assert new_path.exists()
    assert not legacy_log_path.exists()


def test_job_logs_missing_file_sets_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Config.from_env will succeed but LogReader will return no file
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache WITHOUT log_path to test the "log not found" path
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache

    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=None,  # No log path means LogNotFound
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID])

    assert result.exit_code == EXIT_LOG_NOT_FOUND
    assert f"No log file found for job {TEST_JOB_ID}" in result.output


# ---------------------------------------------------------------------------
# Resources / nodes / config commands
# ---------------------------------------------------------------------------


def test_nodes_list_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Include test compute groups in config
    patch_config_and_auth(monkeypatch, tmp_path, include_compute_groups=True)
    from inspire.cli.utils import browser_api as browser_api_module

    test_group_id = "lcg-test000-0000-0000-0000-000000000000"
    monkeypatch.setattr(
        browser_api_module,
        "get_full_free_node_counts",
        lambda group_ids, gpu_per_node=8, session=None, _retry=True: [  # noqa: ARG005
            browser_api_module.FullFreeNodeCount(
                group_id=test_group_id,
                group_name="H200 TestRoom",
                gpu_per_node=gpu_per_node,
                total_nodes=10,
                ready_nodes=8,
                full_free_nodes=3,
            )
        ],
    )
    # Also mock get_accurate_gpu_availability which is called by the nodes command
    monkeypatch.setattr(
        browser_api_module,
        "get_accurate_gpu_availability",
        lambda workspace_id=None, session=None, _retry=True: [  # noqa: ARG005
            browser_api_module.GPUAvailability(
                group_id=test_group_id,
                group_name="H200 TestRoom",
                gpu_type="H200",
                total_gpus=80,
                used_gpus=68,
                available_gpus=12,
                low_priority_gpus=0,
            )
        ],
    )
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "resources", "nodes"])
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert data["data"]["groups"]
    assert data["data"]["total_full_free_nodes"] == 3


def test_config_check_auth_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = make_test_config(tmp_path)

    def fake_from_env(cls, require_target_dir: bool = False) -> config_module.Config:  # type: ignore[override]
        return config

    def fake_from_files_and_env(cls, require_target_dir: bool = False, require_credentials: bool = True) -> tuple:  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))
    monkeypatch.setattr(config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env))

    def fake_get_api(self_or_cls, cfg: Optional[config_module.Config] = None):  # type: ignore[override]
        from inspire.cli.utils.auth import AuthenticationError

        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(auth_module.AuthManager, "get_api", fake_get_api)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["config", "check"])

    assert result.exit_code == EXIT_AUTH_ERROR
    assert "Authentication failed" in result.output


def test_config_check_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        raise ConfigError("missing env")

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"
