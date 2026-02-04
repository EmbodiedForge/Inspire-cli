"""Helpers for caching and offsets in `inspire job logs` (single-job mode)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from inspire.config import Config


class _JobCacheProtocol(Protocol):
    def get_log_offset(self, job_id: str) -> int: ...

    def reset_log_offset(self, job_id: str) -> None: ...

    def set_log_offset(self, job_id: str, offset: int) -> None: ...


@dataclass(frozen=True)
class JobLogCachePaths:
    cache_path: Path
    legacy_cache_path: Path


def build_log_cache_paths(config: Config, job_id: str) -> JobLogCachePaths:
    """Return cache paths for a job log, ensuring the cache directory exists."""
    cache_dir = Path(os.path.expanduser(config.log_cache_dir))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return JobLogCachePaths(
        cache_path=cache_dir / f"{job_id}.log",
        legacy_cache_path=cache_dir / f"job-{job_id}.log",
    )


def migrate_legacy_log_filename(paths: JobLogCachePaths) -> Path:
    """Rename `job-{job_id}.log` -> `{job_id}.log` if needed, returning the active cache path."""
    cache_path = paths.cache_path
    legacy_cache_path = paths.legacy_cache_path

    if not cache_path.exists() and legacy_cache_path.exists():
        try:
            legacy_cache_path.replace(cache_path)
            return cache_path
        except OSError:
            return legacy_cache_path

    return cache_path


def get_current_log_offset(
    cache: _JobCacheProtocol,
    *,
    job_id: str,
    cache_path: Path,
    refresh: bool,
) -> int:
    """Return the cached byte offset, resetting it if the cache file is missing."""
    current_offset = 0 if refresh else cache.get_log_offset(job_id)

    if current_offset > 0 and not cache_path.exists():
        cache.reset_log_offset(job_id)
        return 0

    return current_offset


def update_log_offset_to_filesize(
    cache: _JobCacheProtocol, *, job_id: str, cache_path: Path
) -> None:
    """Update the cached byte offset to the local cache file size."""
    if cache_path.exists():
        cache.set_log_offset(job_id, cache_path.stat().st_size)
