"""Browser (web-session) APIs for notebooks.

This module currently re-exports notebook functions from the legacy implementation.
It exists to provide a stable import surface while the implementation is being split.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_legacy import (
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
