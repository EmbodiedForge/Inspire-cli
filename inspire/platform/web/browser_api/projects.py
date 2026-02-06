"""Browser (web-session) APIs for projects.

Projects are required for both training jobs and notebooks. The web UI exposes a
project listing endpoint with quota information that is not part of the OpenAPI
surface; this module contains the SSO-only implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "ProjectInfo",
    "list_projects",
    "select_project",
]


@dataclass
class ProjectInfo:
    """Project information with quota details."""

    project_id: str
    name: str
    workspace_id: str
    # Quota fields
    budget: float = 0.0  # Total budget allocated
    remain_budget: float = 0.0  # Remaining budget
    member_remain_budget: float = 0.0  # Remaining budget for current user
    member_remain_gpu_hours: float = 0.0  # Remaining GPU hours (negative = over quota)
    gpu_limit: bool = False  # Whether GPU limits are enforced
    member_gpu_limit: bool = False  # Whether member GPU limits are enforced
    priority_level: str = ""  # Priority level (HIGH, NORMAL, etc.)
    priority_name: str = ""  # Priority name (numeric string like "10", "4")

    def has_quota(self) -> bool:
        """Check if the project has available quota."""
        if not self.gpu_limit and not self.member_gpu_limit:
            return True
        return self.member_remain_gpu_hours >= 0

    def get_quota_status(self) -> str:
        """Get formatted quota status string for display."""
        if not self.has_quota():
            return " (over quota)"
        if self.member_gpu_limit:
            return f" ({self.member_remain_gpu_hours:.0f} GPU-hours remaining)"
        return ""


def list_projects(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[ProjectInfo]:
    """List available projects."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page": 1,
        "page_size": -1,
        "filter": {
            "workspace_id": workspace_id,
            "check_admin": True,
        },
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/project/list"),
        referer=f"{_get_base_url()}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    items = data.get("data", {}).get("items", [])

    def _parse_float(value) -> float:
        if value is None or value == "":
            return 0.0
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    return [
        ProjectInfo(
            project_id=item.get("id", ""),
            name=item.get("name", ""),
            workspace_id=item.get("workspace_id", workspace_id),
            budget=_parse_float(item.get("budget")),
            remain_budget=_parse_float(item.get("remain_budget")),
            member_remain_budget=_parse_float(item.get("member_remain_budget")),
            member_remain_gpu_hours=_parse_float(item.get("member_remain_gpu_hours")),
            gpu_limit=bool(item.get("gpu_limit", False)),
            member_gpu_limit=bool(item.get("member_gpu_limit", False)),
            priority_level=item.get("priority_level", ""),
            priority_name=item.get("priority_name", ""),
        )
        for item in items
    ]


def select_project(
    projects: list[ProjectInfo],
    requested: Optional[str] = None,
) -> tuple[ProjectInfo, Optional[str]]:
    """Select a project, with auto-fallback if over quota."""

    def sort_key(p: ProjectInfo) -> tuple:
        has_quota = p.has_quota()
        try:
            priority = int(p.priority_name) if p.priority_name else 0
        except ValueError:
            priority = 0
        return (not has_quota, -priority, p.name)

    if requested:
        target = None
        for project in projects:
            if project.name.lower() == requested.lower() or project.project_id == requested:
                target = project
                break

        if not target:
            raise ValueError(f"Project '{requested}' not found")

        if target.has_quota():
            return (target, None)

        fallback_msg = f"Project '{target.name}' is over quota, selecting alternative..."
        sorted_projects = sorted(projects, key=sort_key)
        fallback = sorted_projects[0]

        if not fallback.has_quota():
            raise ValueError("All projects are over quota")

        return (fallback, fallback_msg)

    sorted_projects = sorted(projects, key=sort_key)
    return (sorted_projects[0], None)
