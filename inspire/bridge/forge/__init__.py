"""Git forge abstraction for GitHub and Gitea Actions.

This module provides a unified interface for interacting with both
GitHub Actions and Gitea Actions APIs, which are largely compatible
but have some differences in authentication and endpoints.

The factory function create_forge_client() returns the appropriate
client based on the configured platform.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from inspire.config import Config
from .artifacts import (
    _find_artifact_by_name,
    download_bridge_artifact,
    fetch_bridge_output_log,
    wait_for_log_artifact,
)
from .clients import (
    ForgeClient,
    GiteaClient,
    GitHubClient,
    create_forge_client,
)
from .config import (
    _get_active_repo,
    _get_active_server,
    _get_active_token,
    _get_active_workflow_file,
    _resolve_platform,
    _sanitize_token,
)
from .helpers import (
    _artifact_name,
    _extract_total_count,
    _find_run_by_inputs,
    _matches_inputs,
    _parse_event_inputs,
)
from .logs import (
    _prune_old_logs,
    fetch_remote_log_incremental,
    fetch_remote_log_via_bridge,
)
from .models import (
    ForgeAuthError,
    ForgeError,
    GitPlatform,
    GiteaAuthError,
    GiteaError,
)
from .workflows import (
    get_workflow_run,
    get_workflow_runs,
    trigger_bridge_action_workflow,
    trigger_log_retrieval_workflow,
    trigger_sync_workflow,
    trigger_workflow_dispatch,
    wait_for_workflow_completion,
)

logger = logging.getLogger(__name__)


def wait_for_bridge_action_completion(
    config: Config,
    request_id: str,
    timeout: Optional[int] = None,
) -> dict:
    """Poll for bridge action workflow completion."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    timeout_seconds = int(timeout) if timeout is not None else int(config.bridge_action_timeout)
    deadline = time.time() + max(5, int(timeout_seconds))

    limit = 20

    def _find_matching_run(runs_list: list) -> Optional[dict]:
        run = _find_run_by_inputs(runs_list, {"request_id": request_id})
        if not run:
            return None
        status = run.get("status")
        conclusion = run.get("conclusion")
        logger.debug(
            "Found matching run: status=%s, conclusion=%s",
            status,
            conclusion,
        )
        # Both platforms use 'success'/'failure' as status or 'completed'
        if status in ("completed", "success", "failure"):
            return {
                "status": status,
                "conclusion": conclusion or status,
                "run_id": run.get("id"),
                "html_url": run.get("html_url", ""),
            }
        return None

    while True:
        if time.time() > deadline:
            raise TimeoutError(f"Bridge action timed out after {timeout_seconds} seconds.")

        try:
            runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, 1)}"
            response = client.request_json("GET", runs_url)
            runs = response.get("workflow_runs", []) or []

            match = _find_matching_run(runs)
            if match:
                return match

            # Some forges return runs in ascending order; check last page
            total_count = _extract_total_count(response)
            if total_count and total_count > limit:
                last_page = (total_count + limit - 1) // limit
                runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, last_page)}"
                response = client.request_json("GET", runs_url)
                runs = response.get("workflow_runs", []) or []
                match = _find_matching_run(runs)
                if match:
                    return match
        except ForgeError:
            pass

        time.sleep(3)


__all__ = [
    # Models / errors
    "GitPlatform",
    "ForgeAuthError",
    "ForgeError",
    "GiteaAuthError",
    "GiteaError",
    # Config / platform resolution
    "_sanitize_token",
    "_resolve_platform",
    "_get_active_repo",
    "_get_active_token",
    "_get_active_server",
    "_get_active_workflow_file",
    # Clients
    "ForgeClient",
    "GiteaClient",
    "GitHubClient",
    "create_forge_client",
    # Helpers
    "_extract_total_count",
    "_parse_event_inputs",
    "_matches_inputs",
    "_find_run_by_inputs",
    "_artifact_name",
    "_find_artifact_by_name",
    "_prune_old_logs",
    # Workflows
    "trigger_workflow_dispatch",
    "trigger_log_retrieval_workflow",
    "trigger_sync_workflow",
    "trigger_bridge_action_workflow",
    "get_workflow_runs",
    "get_workflow_run",
    "wait_for_workflow_completion",
    # Artifacts / logs
    "wait_for_log_artifact",
    "fetch_remote_log_via_bridge",
    "fetch_remote_log_incremental",
    "wait_for_bridge_action_completion",
    "download_bridge_artifact",
    "fetch_bridge_output_log",
]
