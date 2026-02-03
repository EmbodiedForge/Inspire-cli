"""Browser (web-session) APIs for notebooks.

Historically notebook-related SSO endpoints and Playwright flows lived in one large module.
The implementation is now split into smaller modules; this file re-exports the public API
to keep import paths stable.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_notebooks_http import (
    ImageInfo,
    create_notebook,
    get_notebook_detail,
    get_notebook_schedule,
    list_images,
    list_notebook_compute_groups,
    start_notebook,
    stop_notebook,
    wait_for_notebook_running,
)
from inspire.cli.utils.browser_api_notebooks_playwright import (
    run_command_in_notebook,
    setup_notebook_rtunnel,
)

__all__ = [
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
