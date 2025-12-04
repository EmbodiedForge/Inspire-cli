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
)
from inspire.cli.utils import config as config_module
from inspire.cli.utils import auth as auth_module
from inspire.cli.utils.config import ConfigError
from inspire.inspire_api_control import ResourceManager, GPUType


def make_test_config(tmp_path: Path) -> config_module.Config:
    return config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )


class DummyAPI:
    def __init__(self) -> None:
        self.calls: Dict[str, Any] = {}
        self.resource_manager = ResourceManager()

    # Job-related methods -------------------------------------------------
    def create_training_job_smart(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls["create_training_job_smart"] = kwargs
        return {"data": {"job_id": "job-123"}}

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
    def list_available_specs(self, compute_group_id: str) -> Dict[str, Any]:
        self.calls.setdefault("list_available_specs", []).append(compute_group_id)
        return {
            "data": {
                "specs": [
                    {"name": "spec-1", "gpu_count": 1, "cpu_cores": 4, "memory_gb": 32}
                ]
            }
        }

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


def patch_config_and_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DummyAPI:
    """Patch Config.from_env and AuthManager.get_api to use local stubs."""
    config = make_test_config(tmp_path)
    config.target_dir and Path(config.target_dir).mkdir(parents=True, exist_ok=True)

    def fake_from_env(cls, require_target_dir: bool = False) -> config_module.Config:  # type: ignore[override]
        if require_target_dir and not config.target_dir:
            raise ConfigError("Missing INSP_TARGET_DIR")
        return config

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))

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
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "resources", "list"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["success"] is True
    assert "specs" in payload["data"]
    assert "compute_groups" in payload["data"]


def test_global_debug_flag_runs_subcommand(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--debug", "resources", "list"])
    assert result.exit_code == 0


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
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["data"]["job_id"] == "job-123"


def test_job_status_updates_cache_and_formats(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["job", "status", "job-xyz"])
    assert result.exit_code == 0
    assert "Job Status" in result.output
    assert "job-xyz" in result.output


def test_job_status_not_found_sets_specific_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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
        ["--json", "job", "stop", "job-123", "--force"],
    )
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert data["data"]["job_id"] == "job-123"
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
        ["job", "wait", "job-999", "--timeout", "60", "--interval", "1"],
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
        ["job", "wait", "job-123", "--timeout", "1", "--interval", "1"],
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

        def list_jobs(self, limit: int = 10, status: Optional[str] = None) -> List[Dict[str, Any]]:
            return [
                {
                    "job_id": "job-1",
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


def test_job_logs_path_and_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id="job-abc",
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=str(tmp_path / "logs" / "training_master_job-abc.log"),
    )

    # Prepare a dummy log file
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / "training_master_job-abc.log"
    log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")

    from importlib import import_module

    job_cmd = import_module("inspire.cli.commands.job")
    from inspire.cli.utils.logs import LogReader

    # Use real LogReader but force it to find the specific file
    class FakeReader(LogReader):
        def find_latest_log(self, job_id: Optional[str] = None):  # noqa: ARG002
            return log_path

    monkeypatch.setattr(job_cmd, "LogReader", FakeReader)

    runner = CliRunner()

    # --path just prints path
    result = runner.invoke(cli_main, ["job", "logs", "job-abc", "--path"])
    assert result.exit_code == 0
    assert str(log_path) in result.output

    # --tail reads last N lines
    result_tail = runner.invoke(cli_main, ["job", "logs", "job-abc", "--tail", "2"])
    assert result_tail.exit_code == 0
    assert "line2" in result_tail.output
    assert "line3" in result_tail.output


def test_job_logs_follow_with_json_is_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id="job-123",
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=str(tmp_path / "logs" / "training_master_job-123.log"),
    )

    from importlib import import_module

    job_cmd = import_module("inspire.cli.commands.job")
    from inspire.cli.utils.logs import LogReader

    class FakeReader(LogReader):
        def find_latest_log(self, job_id: Optional[str] = None):  # noqa: ARG002
            return Path("dummy.log")

    monkeypatch.setattr(job_cmd, "LogReader", FakeReader)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "job", "logs", "job-123", "--follow"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    data = json.loads(result.output)
    assert data["success"] is False


def test_job_logs_missing_file_sets_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Config.from_env will succeed but LogReader will return no file
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache WITHOUT log_path to test the "log not found" path
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id="job-123",
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=None,  # No log path means LogNotFound
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", "job-123"])

    assert result.exit_code == EXIT_LOG_NOT_FOUND
    assert "No log file found for job job-123" in result.output


# ---------------------------------------------------------------------------
# Resources / nodes / config commands
# ---------------------------------------------------------------------------


def test_resources_check_json_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    # Use a simplified resource manager to ensure deterministic config
    rm = ResourceManager()

    def fake_get_recommended_config(resource_str: str, prefer_location: Optional[str] = None):  # noqa: ARG002
        # Always return a valid group with same GPU type
        return rm.resource_specs[0].spec_id, rm.compute_groups[0].compute_group_id

    rm.get_recommended_config = fake_get_recommended_config  # type: ignore[assignment]
    api.resource_manager = rm

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "resources", "check", "H200"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"]["gpu_type"] == GPUType.H200.value
    assert data["data"]["available_specs"]


def test_resources_check_validation_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    class FailingRM(ResourceManager):
        def get_recommended_config(self, resource_str: str, prefer_location: Optional[str] = None):  # noqa: ARG002
            raise ValueError("invalid resource")

    api.resource_manager = FailingRM()

    runner = CliRunner()
    result = runner.invoke(cli_main, ["resources", "check", "bad"])

    assert result.exit_code == EXIT_API_ERROR
    assert "invalid resource" in result.output


def test_nodes_list_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "nodes", "list", "--pool", "online"])
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert data["data"]["nodes"]
    assert data["data"]["pool_filter"] == "online"


def test_config_check_auth_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = make_test_config(tmp_path)

    def fake_from_env(cls, require_target_dir: bool = False) -> config_module.Config:  # type: ignore[override]
        return config

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))

    def fake_get_api(self_or_cls, cfg: Optional[config_module.Config] = None):  # type: ignore[override]
        from inspire.cli.utils.auth import AuthenticationError

        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(auth_module.AuthManager, "get_api", fake_get_api)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["config", "check"])

    assert result.exit_code == EXIT_AUTH_ERROR
    assert "Authentication failed" in result.output


def test_config_check_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def fake_from_env(cls, require_target_dir: bool = False):  # type: ignore[override]
        raise ConfigError("missing env")

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"
