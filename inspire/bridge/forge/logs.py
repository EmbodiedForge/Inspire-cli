"""High-level helpers for fetching and caching remote logs via forge workflows."""

from __future__ import annotations

import os
import time
from pathlib import Path

from inspire.config import Config

from .artifacts import wait_for_log_artifact
from .workflows import trigger_log_retrieval_workflow


def _prune_old_logs(cache_dir: Path, max_age_days: int = 7) -> None:
    """Remove log files older than max_age_days from the cache directory."""
    if not cache_dir.exists():
        return

    now = time.time()
    max_age_seconds = max_age_days * 24 * 3600

    try:
        for log_file in cache_dir.glob("*.log"):
            if not log_file.is_file():
                continue
            age_seconds = now - log_file.stat().st_mtime
            if age_seconds > max_age_seconds:
                try:
                    log_file.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def fetch_remote_log_via_bridge(
    config: Config,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    refresh: bool = False,
) -> Path:
    """High-level helper to ensure a local cached copy of a remote log."""
    if cache_path.exists() and not refresh:
        return cache_path

    request_id = f"{int(time.time())}-{os.getpid()}"

    trigger_log_retrieval_workflow(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        request_id=request_id,
    )

    wait_for_log_artifact(
        config=config,
        job_id=job_id,
        request_id=request_id,
        cache_path=cache_path,
    )

    cache_dir = cache_path.parent
    _prune_old_logs(cache_dir, max_age_days=7)

    return cache_path


def fetch_remote_log_incremental(
    config: Config,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    start_offset: int = 0,
) -> tuple[Path, int]:
    """Fetch incremental portion of remote log and append to cache.

    Args:
        config: CLI configuration
        job_id: Inspire job ID
        remote_log_path: Absolute path to log on shared filesystem
        cache_path: Local cache file path
        start_offset: Byte offset to start from

    Returns:
        Tuple of (cache_path, bytes_written)

    Raises:
        ForgeError: If workflow fails or artifact not found
        TimeoutError: If workflow times out
    """
    request_id = f"{int(time.time())}-{os.getpid()}"

    # Trigger workflow with offset
    trigger_log_retrieval_workflow(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        request_id=request_id,
        start_offset=start_offset,
    )

    # Download to temp file first
    temp_path = cache_path.parent / f"{job_id}.tmp.{os.getpid()}"
    try:
        wait_for_log_artifact(
            config=config,
            job_id=job_id,
            request_id=request_id,
            cache_path=temp_path,
        )

        # Get bytes written
        bytes_written = temp_path.stat().st_size if temp_path.exists() else 0

        if bytes_written > 0:
            # Append to existing cache
            if cache_path.exists() and start_offset > 0:
                with cache_path.open("ab") as dst:
                    dst.write(temp_path.read_bytes())
            else:
                # First fetch or offset=0, replace file
                temp_path.replace(cache_path)
                return cache_path, bytes_written

        return cache_path, bytes_written
    finally:
        # Cleanup temp file
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
