"""Browser (web-session) API façade.

Historically all SSO-only endpoints lived in one large module. The implementation is now split
into smaller domain modules, and this file re-exports the public API to keep import paths stable.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_availability import (
    FullFreeNodeCount,
    GPUAvailability,
    find_best_compute_group_accurate,
    get_accurate_gpu_availability,
    get_full_free_node_counts,
    list_compute_groups,
)
from inspire.cli.utils.browser_api_jobs import (
    JobInfo,
    get_current_user,
    list_job_users,
    list_jobs,
)
from inspire.cli.utils.browser_api_notebooks import (  # noqa: F401
    ImageInfo,
    create_notebook,
    get_notebook_detail,
    get_notebook_schedule,
    list_images,
    list_notebook_compute_groups,
    run_command_in_notebook,
    setup_notebook_rtunnel,
    start_notebook,
    stop_notebook,
    wait_for_notebook_running,
)
from inspire.cli.utils.browser_api_projects import (
    ProjectInfo,
    list_projects,
    select_project,
)

__all__ = [
    # Jobs / users
    "JobInfo",
    "get_current_user",
    "list_job_users",
    "list_jobs",
    # Availability
    "FullFreeNodeCount",
    "GPUAvailability",
    "find_best_compute_group_accurate",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "list_compute_groups",
    # Projects
    "ProjectInfo",
    "list_projects",
    "select_project",
    # Notebooks
    "ImageInfo",
    "create_notebook",
    "get_notebook_detail",
    "get_notebook_schedule",
    "list_images",
    "list_notebook_compute_groups",
    "run_command_in_notebook",
    "setup_notebook_rtunnel",
    "start_notebook",
    "stop_notebook",
    "wait_for_notebook_running",
]
