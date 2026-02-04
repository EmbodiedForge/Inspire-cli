"""Browser (web-session) notebook APIs (HTTP endpoints only).

This module is kept as a façade and re-exports from smaller `browser_api_notebooks_*` modules.
"""

from __future__ import annotations

from inspire.cli.utils._impl.browser_api.notebooks.http.api import (  # noqa: F401
    create_notebook,
    get_notebook_detail,
    get_notebook_schedule,
    list_images,
    list_notebook_compute_groups,
    start_notebook,
    stop_notebook,
)
from inspire.cli.utils._impl.browser_api.notebooks.http.wait import (  # noqa: F401
    wait_for_notebook_running,
)
from inspire.cli.utils._impl.browser_api.notebooks.http.models import ImageInfo  # noqa: F401

__all__ = [
    "ImageInfo",
    "create_notebook",
    "get_notebook_detail",
    "get_notebook_schedule",
    "list_images",
    "list_notebook_compute_groups",
    "start_notebook",
    "stop_notebook",
    "wait_for_notebook_running",
]
