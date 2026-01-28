"""Gitea helpers for remote job log retrieval and bridge operations.

.. deprecated::
    This module is now a thin wrapper around forge.py for backward compatibility.
    New code should use inspire.cli.utils.forge directly.

This module provides a wrapper around the Gitea REST API to trigger
workflows on a Bridge runner for log retrieval, code sync, and
arbitrary command execution.

Gitea Actions API is compatible with GitHub Actions, so this module
is similar to the original GitHub implementation.
"""

from __future__ import annotations

# Re-export all public API from forge.py for backward compatibility
from inspire.cli.utils.forge import (
    # Abstract base and clients
    ForgeClient,
    ForgeAuthError,
    ForgeError,
    GiteaClient,
    GitHubClient,
    GitPlatform,
    # Factory and helpers
    _sanitize_token,
    create_forge_client,
    _resolve_platform,
    _get_active_repo,
    _get_active_token,
    _get_active_server,
    _get_active_workflow_file,
    # Internal helpers (kept for backward compatibility with tests)
    _extract_total_count,
    _parse_event_inputs,
    _matches_inputs,
    _find_run_by_inputs,
    _artifact_name,
    _find_artifact_by_name,
    _prune_old_logs,
    # Public API functions
    trigger_workflow_dispatch,
    trigger_log_retrieval_workflow,
    trigger_sync_workflow,
    trigger_bridge_action_workflow,
    get_workflow_runs,
    get_workflow_run,
    wait_for_workflow_completion,
    wait_for_log_artifact,
    fetch_remote_log_via_bridge,
    fetch_remote_log_incremental,
    wait_for_bridge_action_completion,
    download_bridge_artifact,
    fetch_bridge_output_log,
    # Exception aliases for backward compatibility
    GiteaAuthError as _GiteaAuthError,
    GiteaError as _GiteaError,
)

# Re-export exceptions with old names for complete backward compatibility
GiteaAuthError = _GiteaAuthError
GiteaError = _GiteaError

# Module-level constants (backward compatibility)
__all__ = [
    # Classes
    "ForgeClient",
    "GiteaClient",
    "GitHubClient",
    "GitPlatform",
    # Exceptions
    "ForgeAuthError",
    "ForgeError",
    "GiteaAuthError",
    "GiteaError",
    # Factory
    "create_forge_client",
    # Public API
    "trigger_workflow_dispatch",
    "trigger_log_retrieval_workflow",
    "trigger_sync_workflow",
    "trigger_bridge_action_workflow",
    "get_workflow_runs",
    "get_workflow_run",
    "wait_for_workflow_completion",
    "wait_for_log_artifact",
    "fetch_remote_log_via_bridge",
    "fetch_remote_log_incremental",
    "wait_for_bridge_action_completion",
    "download_bridge_artifact",
    "fetch_bridge_output_log",
    # Internal helpers (exposed for testing)
    "_sanitize_token",
    "_resolve_platform",
    "_get_active_repo",
    "_get_active_token",
    "_get_active_server",
    "_get_active_workflow_file",
    "_extract_total_count",
    "_parse_event_inputs",
    "_matches_inputs",
    "_find_run_by_inputs",
    "_artifact_name",
    "_find_artifact_by_name",
    "_prune_old_logs",
]
