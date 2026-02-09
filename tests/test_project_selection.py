"""Tests for project selection behavior."""

from __future__ import annotations

import pytest

from inspire.config import Config
from inspire.cli.commands.notebook import notebook_create_flow
from inspire.platform.web.browser_api.projects import ProjectInfo, select_project


def _project(
    project_id: str,
    name: str,
    *,
    member_gpu_limit: bool,
    member_remain_gpu_hours: float,
    priority_name: str,
) -> ProjectInfo:
    return ProjectInfo(
        project_id=project_id,
        name=name,
        workspace_id="ws-test",
        member_gpu_limit=member_gpu_limit,
        member_remain_gpu_hours=member_remain_gpu_hours,
        priority_name=priority_name,
    )


def test_select_project_requested_over_quota_falls_back_by_default() -> None:
    requested = _project(
        "project-requested",
        "Requested Project",
        member_gpu_limit=True,
        member_remain_gpu_hours=-10.0,
        priority_name="10",
    )
    fallback = _project(
        "project-fallback",
        "Fallback Project",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="4",
    )

    selected, message = select_project(
        [requested, fallback],
        requested="project-requested",
    )

    assert selected.project_id == "project-fallback"
    assert message is not None
    assert "over quota" in message


def test_select_project_requested_over_quota_allowed_for_cpu() -> None:
    requested = _project(
        "project-requested",
        "Requested Project",
        member_gpu_limit=True,
        member_remain_gpu_hours=-10.0,
        priority_name="10",
    )
    fallback = _project(
        "project-fallback",
        "Fallback Project",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="4",
    )

    selected, message = select_project(
        [requested, fallback],
        requested="project-requested",
        needs_gpu_quota=False,
    )

    assert selected.project_id == "project-requested"
    assert message is None


def test_select_project_requested_over_quota_can_be_forced_to_proceed() -> None:
    requested = _project(
        "project-requested",
        "Requested Project",
        member_gpu_limit=True,
        member_remain_gpu_hours=-10.0,
        priority_name="10",
    )
    fallback = _project(
        "project-fallback",
        "Fallback Project",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="4",
    )

    selected, message = select_project(
        [requested, fallback],
        requested="project-requested",
        allow_requested_over_quota=True,
    )

    assert selected.project_id == "project-requested"
    assert message is not None
    assert "continuing" in message


def test_resolve_notebook_project_passes_quota_and_shared_path_settings(monkeypatch) -> None:
    requested = _project(
        "project-requested",
        "Requested Project",
        member_gpu_limit=True,
        member_remain_gpu_hours=-10.0,
        priority_name="10",
    )
    called: dict[str, object] = {}

    def fake_select_project(
        projects,
        requested_value=None,
        *,
        allow_requested_over_quota=False,
        shared_path_group_by_id=None,
        needs_gpu_quota=True,
    ):
        called["requested"] = requested_value
        called["allow_requested_over_quota"] = allow_requested_over_quota
        called["shared_path_group_by_id"] = shared_path_group_by_id
        called["needs_gpu_quota"] = needs_gpu_quota
        return requested, None

    monkeypatch.setattr(
        notebook_create_flow.browser_api_module, "select_project", fake_select_project
    )

    config = Config(username="user", password="pass")

    resolved = notebook_create_flow.resolve_notebook_project(
        notebook_create_flow.Context(),
        projects=[requested],
        config=config,
        project="project-requested",
        allow_requested_over_quota=True,
        needs_gpu_quota=False,
        json_output=True,
    )

    assert resolved is requested
    assert called["requested"] == "project-requested"
    assert called["allow_requested_over_quota"] is True
    assert called["shared_path_group_by_id"] is None
    assert called["needs_gpu_quota"] is False


def test_select_project_filters_known_incompatible_shared_path_group() -> None:
    requested = _project(
        "project-requested",
        "Requested",
        member_gpu_limit=True,
        member_remain_gpu_hours=-1.0,
        priority_name="10",
    )
    incompatible = _project(
        "project-incompatible",
        "Incompatible",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="4",
    )
    unknown = _project(
        "project-unknown",
        "Unknown",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="6",
    )

    selected, message = select_project(
        [requested, incompatible, unknown],
        requested="project-requested",
        shared_path_group_by_id={
            "project-requested": "/train/global_user/u",
            "project-incompatible": "/train/global_user/other",
        },
    )

    assert selected.project_id == "project-unknown"
    assert message is not None
    assert "unknown shared-path" in message


def test_select_project_shared_path_group_excludes_all_raises() -> None:
    requested = _project(
        "project-requested",
        "Requested",
        member_gpu_limit=True,
        member_remain_gpu_hours=-1.0,
        priority_name="10",
    )
    incompatible = _project(
        "project-incompatible",
        "Incompatible",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="4",
    )

    with pytest.raises(ValueError, match="shared-path mismatch"):
        select_project(
            [requested, incompatible],
            requested="project-requested",
            shared_path_group_by_id={
                "project-requested": "/train/global_user/u",
                "project-incompatible": "/train/global_user/other",
            },
        )
