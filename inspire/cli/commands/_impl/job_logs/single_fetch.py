"""Fetch helpers for `inspire job logs` (single-job mode)."""

from __future__ import annotations

from pathlib import Path

from inspire.cli.utils.config import Config
from inspire.cli.utils.gitea import fetch_remote_log_incremental


def fetch_log_incremental(
    *,
    config: Config,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    start_offset: int,
) -> int:
    """Append new bytes from the remote log to the local cache file, returning bytes added."""
    _, bytes_added = fetch_remote_log_incremental(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        cache_path=cache_path,
        start_offset=start_offset,
    )
    return bytes_added


def fetch_log_full_via_bridge(
    *,
    deps,
    config: Config,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    refresh: bool,
) -> None:
    """Fetch the full remote log via the Bridge workflow."""
    deps.fetch_remote_log_via_bridge(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        cache_path=cache_path,
        refresh=refresh,
    )


def format_remote_log_error_message(err: Exception, *, remote_log_path: str, config: Config) -> str:
    return (
        f"{str(err)}\n\n"
        f"Hints:\n"
        f"- Check that the training job created a log file at: {remote_log_path}\n"
        f"- Verify the Bridge workflow exists and can access the shared filesystem\n"
        f"- View Gitea Actions at: {config.gitea_server}/{config.gitea_repo}/actions"
    )
