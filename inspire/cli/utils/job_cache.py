"""Local job cache for tracking submitted jobs.

Since the Inspire API has no "list jobs" endpoint, we maintain a local
cache of submitted jobs for the `inspire job list` command.
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any


logger = logging.getLogger(__name__)


class JobCache:
    """Local cache for tracking submitted jobs.

    Jobs are stored in a JSON file with the structure:
    {
        "job-id-1": {
            "name": "pr-123-debug",
            "resource": "4xH200",
            "command": "bash train.sh",
            "created_at": "2025-01-15T10:30:00",
            "status": "RUNNING",
            "updated_at": "2025-01-15T10:35:00"
        },
        ...
    }
    """

    def __init__(self, cache_path: Optional[str] = None):
        """Initialize job cache.

        Args:
            cache_path: Path to cache file. Defaults to ~/.inspire/jobs.json
        """
        if cache_path:
            self.cache_path = Path(os.path.expanduser(cache_path))
        else:
            self.cache_path = Path.home() / ".inspire" / "jobs.json"

        # Ensure directory exists
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Load cache from file."""
        if not self.cache_path.exists():
            return {}

        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        """Save cache to file."""
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(jobs, f, indent=2, ensure_ascii=False)
        except IOError as e:
            # Log but don't fail - cache is optional
            logger.warning("Failed to write job cache at %s: %s", self.cache_path, e)

    def add_job(
        self,
        job_id: str,
        name: str,
        resource: str,
        command: str,
        status: str = "PENDING",
        log_path: Optional[str] = None,
    ) -> None:
        """Add a newly created job to the cache.

        Args:
            job_id: Unique job identifier
            name: Job name
            resource: Resource specification used
            command: Start command
            status: Initial status (default: PENDING)
        """
        jobs = self._load()
        jobs[job_id] = {
            "name": name,
            "resource": resource,
            "command": command,
            "created_at": datetime.now().isoformat(),
            "status": status,
            "updated_at": datetime.now().isoformat(),
        }
        if log_path is not None:
            jobs[job_id]["log_path"] = log_path
        self._save(jobs)

    def update_status(self, job_id: str, status: str) -> None:
        """Update job status in cache.

        Args:
            job_id: Job identifier
            status: New status
        """
        jobs = self._load()
        if job_id in jobs:
            jobs[job_id]["status"] = status
            jobs[job_id]["updated_at"] = datetime.now().isoformat()
            self._save(jobs)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job info from cache.

        Args:
            job_id: Job identifier

        Returns:
            Job data dict or None if not found
        """
        jobs = self._load()
        if job_id in jobs:
            return {"job_id": job_id, **jobs[job_id]}
        return None

    def list_jobs(
        self,
        limit: int = 10,
        status: Optional[str] = None,
        exclude_statuses: Optional[set] = None
    ) -> List[Dict[str, Any]]:
        """List recent jobs from cache.

        Args:
            limit: Maximum number of jobs to return
            status: Filter by status (optional)
            exclude_statuses: Set of statuses to exclude (optional)

        Returns:
            List of job data dicts, sorted by created_at descending
        """
        jobs = self._load()

        # Convert to list with job_id included
        items = [{"job_id": k, **v} for k, v in jobs.items()]

        # Filter by status if specified
        if status:
            items = [j for j in items if j.get("status") == status]

        # Exclude specified statuses
        if exclude_statuses:
            items = [j for j in items if j.get("status") not in exclude_statuses]

        # Sort by created_at descending (most recent first)
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        if limit is not None and limit > 0:
            return items[:limit]
        return items

    def remove_job(self, job_id: str) -> bool:
        """Remove a job from cache.

        Args:
            job_id: Job identifier

        Returns:
            True if job was removed, False if not found
        """
        jobs = self._load()
        if job_id in jobs:
            del jobs[job_id]
            self._save(jobs)
            return True
        return False

    def clear(self) -> None:
        """Clear all jobs from cache."""
        self._save({})

    def prune(self, max_age_days: int = 30) -> int:
        """Remove old jobs from cache.

        Args:
            max_age_days: Remove jobs older than this many days

        Returns:
            Number of jobs removed
        """
        jobs = self._load()
        now = datetime.now()
        removed = 0

        to_remove = []
        for job_id, job_data in jobs.items():
            try:
                created = datetime.fromisoformat(job_data.get("created_at", ""))
                age_days = (now - created).days
                if age_days > max_age_days:
                    to_remove.append(job_id)
            except (ValueError, TypeError):
                continue

        for job_id in to_remove:
            del jobs[job_id]
            removed += 1

        if removed > 0:
            self._save(jobs)

        return removed

    def get_log_offset(self, job_id: str) -> int:
        """Get the cached byte offset for a job's log.

        Args:
            job_id: Job identifier

        Returns:
            Byte offset (0 if no cache exists or job not found)
        """
        jobs = self._load()
        if job_id in jobs:
            return jobs[job_id].get("log_byte_offset", 0)
        return 0

    def set_log_offset(self, job_id: str, offset: int) -> None:
        """Update the cached byte offset for a job's log.

        Args:
            job_id: Job identifier
            offset: New offset value (total bytes cached)
        """
        jobs = self._load()
        if job_id in jobs:
            jobs[job_id]["log_byte_offset"] = offset
            jobs[job_id]["log_cached_at"] = datetime.now().isoformat()
            self._save(jobs)

    def reset_log_offset(self, job_id: str) -> None:
        """Reset the byte offset for a job's log (used with --refresh).

        Args:
            job_id: Job identifier
        """
        jobs = self._load()
        if job_id in jobs:
            jobs[job_id]["log_byte_offset"] = 0
            if "log_cached_at" in jobs[job_id]:
                del jobs[job_id]["log_cached_at"]
            self._save(jobs)
