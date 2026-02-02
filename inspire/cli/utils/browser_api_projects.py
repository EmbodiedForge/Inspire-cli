"""Browser (web-session) APIs for projects.

This module currently re-exports project functions from the legacy implementation.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_legacy import (
    ProjectInfo,
    list_projects,
    select_project,
)

__all__ = [
    "ProjectInfo",
    "list_projects",
    "select_project",
]
