"""Forge workflow operations (dispatch, list, status)."""

from __future__ import annotations

import time
from typing import Optional

from inspire.cli.utils.config import Config

from .forge_clients import create_forge_client
from .forge_config import _get_active_repo, _get_active_workflow_file
from .forge_helpers import _extract_total_count, _find_run_by_inputs
from .forge_models import ForgeError


def trigger_workflow_dispatch(
    config: Config,
    workflow_file: str,
    inputs: dict,
    ref: str = "main",
) -> dict:
    """Trigger a workflow via workflow_dispatch.

    Args:
        config: CLI configuration
        workflow_file: Workflow filename (e.g., 'sync_code.yml')
        inputs: Workflow inputs
        ref: Git ref to run on (default: main)

    Returns:
        Response dict (may be empty for 204 responses)
    """
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    api_base = client.get_api_base(repo)

    url = f"{api_base}/workflows/{workflow_file}/dispatches"

    data = {
        "ref": ref,
        "inputs": inputs,
    }

    try:
        response = client.request_json("POST", url, data)
        return response
    except ForgeError as e:
        raise ForgeError(f"Failed to trigger workflow: {e}")


def trigger_log_retrieval_workflow(
    config: Config,
    job_id: str,
    remote_log_path: str,
    request_id: str,
    start_offset: int = 0,
) -> None:
    """Trigger the workflow that uploads a job log as an artifact.

    Args:
        config: CLI configuration
        job_id: Inspire job ID
        remote_log_path: Absolute path to log on shared filesystem
        request_id: Unique request identifier
        start_offset: Byte offset to start reading from (default: 0 = full file)
    """
    inputs = {
        "job_id": job_id,
        "remote_log_path": remote_log_path,
        "request_id": request_id,
        "start_offset": str(start_offset),
    }
    workflow_file = _get_active_workflow_file(config, "log")
    trigger_workflow_dispatch(config, workflow_file, inputs)


def trigger_sync_workflow(
    config: Config,
    branch: str,
    commit_sha: str,
    force: bool = False,
) -> str:
    """Trigger the sync workflow.

    Returns the workflow run ID (or empty string if not available).
    """
    inputs = {
        "branch": branch,
        "commit_sha": commit_sha,
        "force": str(force).lower(),
        "target_dir": config.target_dir or "",
    }
    workflow_file = _get_active_workflow_file(config, "sync")
    trigger_workflow_dispatch(config, workflow_file, inputs)

    # Wait briefly and find the run ID
    time.sleep(2)

    repo = _get_active_repo(config)
    client = create_forge_client(config)

    # Build expected inputs for matching
    expected_inputs = {
        "branch": branch,
        "commit_sha": commit_sha,
        "force": str(force).lower(),
        "target_dir": config.target_dir or "",
    }

    limit = 20
    for _ in range(3):
        try:
            # Use platform-specific pagination
            runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, 1)}"
            response = client.request_json("GET", runs_url)
            runs = response.get("workflow_runs", []) or []

            run = _find_run_by_inputs(runs, expected_inputs)
            if run:
                return str(run.get("id", ""))

            total_count = _extract_total_count(response)
            if total_count and total_count > limit:
                last_page = (total_count + limit - 1) // limit
                runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, last_page)}"
                response = client.request_json("GET", runs_url)
                runs = response.get("workflow_runs", []) or []
                run = _find_run_by_inputs(runs, expected_inputs)
                if run:
                    return str(run.get("id", ""))
        except ForgeError:
            pass

        time.sleep(1)

    return ""


def trigger_bridge_action_workflow(
    config: Config,
    raw_command: str,
    artifact_paths: list[str],
    request_id: str,
    denylist: Optional[list[str]] = None,
) -> None:
    """Trigger the Bridge action workflow for arbitrary command exec."""
    denylist_str = "\n".join(denylist or [])
    artifact_paths_str = "\n".join(artifact_paths)

    inputs = {
        "raw_command": raw_command,
        "denylist": denylist_str,
        "target_dir": config.target_dir or "",
        "artifact_paths": artifact_paths_str,
        "request_id": request_id,
    }
    workflow_file = _get_active_workflow_file(config, "bridge")
    trigger_workflow_dispatch(config, workflow_file, inputs)


def get_workflow_runs(config: Config, limit: int = 20) -> list:
    """Get recent workflow runs."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, 1)}"

    try:
        response = client.request_json("GET", url)
        return response.get("workflow_runs", []) or []
    except ForgeError as e:
        raise ForgeError(f"Failed to get workflow runs: {e}")


def get_workflow_run(config: Config, run_id: str) -> dict:
    """Get a specific workflow run."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    url = f"{client.get_api_base(repo)}/runs/{run_id}"

    try:
        return client.request_json("GET", url)
    except ForgeError as e:
        raise ForgeError(f"Failed to get workflow run: {e}")


def wait_for_workflow_completion(
    config: Config,
    run_id: str,
    timeout: Optional[int] = None,
) -> dict:
    """Wait for a workflow run to complete."""
    timeout_seconds = timeout or config.remote_timeout or 90
    deadline = time.time() + max(5, int(timeout_seconds))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Workflow timed out after {timeout_seconds} seconds.\n"
                f"To increase the timeout, set: export INSP_REMOTE_TIMEOUT=<seconds>"
            )

        run = get_workflow_run(config, run_id)
        status = run.get("status")
        conclusion = run.get("conclusion")

        # Both Gitea and GitHub use: completed, success, failure
        if status in ("completed", "success", "failure"):
            return {
                "status": status,
                "conclusion": conclusion or status,
                "run_id": run_id,
                "html_url": run.get("html_url", ""),
            }

        time.sleep(3)
