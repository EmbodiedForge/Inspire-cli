"""Legacy browser (web-session) API implementation.

This module is kept for backward compatibility with older import paths.
New code should import via `inspire.cli.utils.browser_api` (façade) or the
domain modules in `inspire.cli.utils.browser_api_*`.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_availability import (  # noqa: F401
    FullFreeNodeCount,
    GPUAvailability,
    find_best_compute_group_accurate,
    get_accurate_gpu_availability,
    get_full_free_node_counts,
    list_compute_groups,
)
from inspire.cli.utils.browser_api_core import (  # noqa: F401
    BASE_URL,
    _browser_api_path,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _request_json,
    _run_in_thread,
)
from inspire.cli.utils.browser_api_jobs import (  # noqa: F401
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
from inspire.cli.utils.browser_api_projects import (  # noqa: F401
    ProjectInfo,
    list_projects,
    select_project,
)
from inspire.cli.utils.web_session import (  # noqa: F401
    DEFAULT_WORKSPACE_ID,
    WebSession,
    build_requests_session,
    get_playwright_proxy,
    get_web_session,
    request_json,
)

__all__ = [
    # web-session / helpers (legacy)
    "BASE_URL",
    "DEFAULT_WORKSPACE_ID",
    "WebSession",
    "build_requests_session",
    "get_playwright_proxy",
    "get_web_session",
    "request_json",
    # core helpers (legacy)
    "_browser_api_path",
    "_in_asyncio_loop",
    "_launch_browser",
    "_new_context",
    "_request_json",
    "_run_in_thread",
    # jobs / users
    "JobInfo",
    "get_current_user",
    "list_job_users",
    "list_jobs",
    # availability
    "FullFreeNodeCount",
    "GPUAvailability",
    "find_best_compute_group_accurate",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "list_compute_groups",
    # projects
    "ProjectInfo",
    "list_projects",
    "select_project",
    # notebooks
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
