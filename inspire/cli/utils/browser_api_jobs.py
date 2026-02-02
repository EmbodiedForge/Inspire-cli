"""Browser (web-session) APIs for jobs and users.

This module currently re-exports job/user functions from the legacy implementation.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_legacy import (
    JobInfo,
    get_current_user,
    list_job_users,
    list_jobs,
)

__all__ = [
    "JobInfo",
    "get_current_user",
    "list_job_users",
    "list_jobs",
]

