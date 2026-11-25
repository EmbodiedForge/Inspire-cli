"""Browser API adapter for platform Ray jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.browser_api.errors import (
    ensure_browser_api_success,
    get_browser_api_action_result,
)
from inspire.platform.web.browser_api.workload_contracts import (
    WorkloadOfferDiscovery,
    discover_workload_offers,
)
from inspire.platform.web.session import WebSession, get_web_session

_RAY_JOB_TYPE = "ray_job"
_RAY_SCHEDULE_CONFIG_TYPE = "SCHEDULE_CONFIG_TYPE_RAY_JOB"


@dataclass(frozen=True)
class RayJobInfo:
    """Summary row returned by the platform Ray list action."""

    ray_job_id: str
    name: str
    status: str
    workspace_id: str
    project_id: str
    project_name: str
    created_at: str
    finished_at: str | None
    created_by_id: str
    created_by_name: str
    priority: int | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "RayJobInfo":
        created_by = data.get("created_by")
        if not isinstance(created_by, dict):
            created_by = {}
        return cls(
            ray_job_id=str(data.get("ray_job_id") or data.get("id") or ""),
            name=str(data.get("name") or data.get("job_name") or ""),
            status=str(data.get("status") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            project_id=str(data.get("project_id") or ""),
            project_name=str(data.get("project_name") or ""),
            created_at=str(data.get("created_at") or ""),
            finished_at=str(data.get("finished_at")) if data.get("finished_at") else None,
            created_by_id=str(created_by.get("id") or data.get("created_by_id") or ""),
            created_by_name=str(created_by.get("name") or data.get("created_by_name") or ""),
            priority=_int_or_none(data.get("priority")),
            raw=dict(data),
        )


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_total(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _ray_referer(workspace_id: str | None = None) -> str:
    base = f"{_get_base_url()}/jobs/ray"
    if workspace_id:
        return f"{base}?{urlencode({'spaceId': workspace_id})}"
    return base


def _ray_detail_referer(ray_job_id: str | None = None) -> str:
    if ray_job_id:
        return f"{_get_base_url()}/jobs/rayDetail/{ray_job_id}"
    return _ray_referer()


def list_ray_jobs(
    *,
    workspace_id: str,
    user_ids: list[str] | None = None,
    page_num: int = 1,
    page_size: int = 50,
    session: WebSession | None = None,
) -> tuple[list[RayJobInfo], int]:
    """List Ray jobs via the platform's current v2 ListJobs action."""
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("workspace_id is required")
    if session is None:
        session = get_web_session()

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page_num": page_num,
        "page_size": page_size,
    }
    if user_ids is not None:
        body["filter_by"] = {
            "user_id": [str(user_id) for user_id in user_ids if str(user_id).strip()]
        }

    response = _request_json(
        session,
        "POST",
        "/api/v2/ray?Action=ListJobs",
        referer=_ray_referer(workspace_id),
        body=body,
        timeout=30,
    )
    result = get_browser_api_action_result(response, action="List Ray jobs")
    raw_jobs = result.get("items", [])
    if not isinstance(raw_jobs, list):
        raise ValueError("List Ray jobs failed: platform items is not a list")
    jobs = [RayJobInfo.from_api_response(item) for item in raw_jobs if isinstance(item, dict)]
    return jobs, _as_total(result.get("total"), len(jobs))


def get_ray_job_detail(
    ray_job_id: str,
    *,
    session: WebSession | None = None,
) -> dict[str, Any]:
    """Fetch one Ray job via the platform's current v2 GetJob action."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        "/api/v2/ray?Action=GetJob",
        referer=_ray_detail_referer(ray_job_id),
        body={"ray_job_id": ray_job_id},
        timeout=30,
    )
    return get_browser_api_action_result(response, action="Get Ray job")


def create_ray_job(
    payload: dict[str, Any],
    *,
    session: WebSession | None = None,
) -> dict[str, Any]:
    """Create a Ray job with an already prepared native platform payload."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload must be a non-empty dict")
    if session is None:
        session = get_web_session()
    response = _request_json(
        session,
        "POST",
        "/api/v2/ray?Action=CreateJob",
        referer=_ray_referer(str(payload.get("workspace_id") or "") or None),
        body=payload,
        timeout=60,
    )
    return get_browser_api_action_result(response, action="Create Ray job")


def stop_ray_job(
    ray_job_id: str,
    *,
    session: WebSession | None = None,
) -> dict[str, Any]:
    """Stop a running Ray job."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")
    if session is None:
        session = get_web_session()
    response = _request_json(
        session,
        "POST",
        "/api/v2/ray?Action=StopJob",
        referer=_ray_detail_referer(ray_job_id),
        body={"ray_job_id": ray_job_id},
        timeout=30,
    )
    return get_browser_api_action_result(response, action="Stop Ray job")


def delete_ray_job(
    ray_job_id: str,
    *,
    session: WebSession | None = None,
) -> dict[str, Any]:
    """Delete a Ray job record."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")
    if session is None:
        session = get_web_session()
    response = _request_json(
        session,
        "POST",
        "/api/v2/ray?Action=DeleteJob",
        referer=_ray_referer(),
        body={"ray_job_id": ray_job_id},
        timeout=30,
    )
    return get_browser_api_action_result(response, action="Delete Ray job")


def list_ray_job_events(
    ray_job_id: str,
    *,
    page_num: int = 1,
    page_size: int = -1,
    session: WebSession | None = None,
) -> list[dict[str, Any]]:
    """List job-level scheduler events for a Ray job."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        _browser_api_path("/ray_job/events/list"),
        referer=_ray_referer(),
        body={
            "ray_job_id": ray_job_id,
            "page_num": page_num,
            "page_size": page_size,
            "sorter": [{"field": "last_timestamp", "sort": "ascend"}],
        },
        timeout=30,
    )
    ensure_browser_api_success(response, action="List Ray job events")
    payload = response.get("data")
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("items", payload.get("list", payload.get("events", [])))
    if not isinstance(raw_items, list):
        raise ValueError("List Ray job events failed: platform items is not a list")
    return [item for item in raw_items if isinstance(item, dict)]


def list_ray_job_instances(
    ray_job_id: str,
    *,
    page_num: int = 1,
    page_size: int = 100,
    session: WebSession | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """List head and worker instances for a Ray job."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        _browser_api_path("/ray_job/instances/list"),
        referer=_ray_referer(),
        body={"ray_job_id": ray_job_id, "page_num": page_num, "page_size": page_size},
        timeout=30,
    )
    ensure_browser_api_success(response, action="List Ray job instances")
    payload = response.get("data")
    if not isinstance(payload, dict):
        return [], 0
    raw_items = payload.get("items", payload.get("list", []))
    if not isinstance(raw_items, list):
        raise ValueError("List Ray job instances failed: platform items is not a list")
    items = [item for item in raw_items if isinstance(item, dict)]
    return items, _as_total(payload.get("total"), len(items))


def list_ray_job_log_instances(
    ray_job_id: str,
    *,
    session: WebSession | None = None,
) -> list[dict[str, Any]]:
    """List current Ray instances via the v2 action used by aggregated logs."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")
    if session is None:
        session = get_web_session()
    response = _request_json(
        session,
        "POST",
        "/api/v2/ray?Action=ListJobInstances",
        referer=_ray_detail_referer(ray_job_id),
        body={"page_size": -1, "ray_job_id": ray_job_id, "page_num": 1},
        timeout=30,
    )
    result = get_browser_api_action_result(response, action="List Ray log instances")
    raw_items = result.get("items", result.get("instances", result.get("list", [])))
    if not isinstance(raw_items, list):
        raise ValueError("List Ray log instances failed: platform items is not a list")
    return [item for item in raw_items if isinstance(item, dict)]


def list_ray_job_logs(
    *,
    pod_names: list[str],
    start_timestamp_ms: int | str,
    end_timestamp_ms: int | str,
    page_size: int = 200,
    ray_job_id: str | None = None,
    session: WebSession | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch Ray aggregated logs through the current v2 GetJobLog action."""
    names = [str(name).strip() for name in pod_names if str(name).strip()]
    if not names:
        raise ValueError("pod_names must contain at least one pod name")
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if session is None:
        session = get_web_session()
    response = _request_json(
        session,
        "POST",
        "/api/v2/ray?Action=GetJobLog",
        referer=_ray_detail_referer(ray_job_id),
        body={
            "page_size": page_size,
            "filter": {
                "podNames": names,
                "start_timestamp_ms": str(start_timestamp_ms),
                "end_timestamp_ms": str(end_timestamp_ms),
            },
            "sorter": [
                {"field": "time", "sort": "descend"},
                {"field": "log-id.keyword", "sort": "descend"},
            ],
        },
        timeout=30,
    )
    result = get_browser_api_action_result(response, action="Get Ray job logs")
    raw_logs = result.get("logs", result.get("items", []))
    if not isinstance(raw_logs, list):
        raise ValueError("Get Ray job logs failed: platform logs is not a list")
    logs = [item for item in raw_logs if isinstance(item, dict)]
    return logs, _as_total(result.get("total"), len(logs))


def list_ray_job_scaling_histories(
    ray_job_id: str,
    *,
    page_num: int = 1,
    page_size: int = 50,
    session: WebSession | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """List worker-group scaling history for a Ray job."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        _browser_api_path("/ray_job/scaling_histories/list"),
        referer=_ray_referer(),
        body={"ray_job_id": ray_job_id, "page_num": page_num, "page_size": page_size},
        timeout=30,
    )
    ensure_browser_api_success(response, action="List Ray job scaling histories")
    payload = response.get("data")
    if not isinstance(payload, dict):
        return [], 0
    raw_items = payload.get("items", payload.get("list", []))
    if not isinstance(raw_items, list):
        raise ValueError("List Ray job scaling histories failed: platform items is not a list")
    items = [item for item in raw_items if isinstance(item, dict)]
    return items, _as_total(payload.get("total"), len(items))


def list_ray_offers(
    *,
    workspace_id: str,
    session: WebSession | None = None,
) -> WorkloadOfferDiscovery:
    """Resolve launchable Ray offers from live capability and quota contracts."""
    return discover_workload_offers(
        workload="ray",
        workspace_id=workspace_id,
        platform_job_type=_RAY_JOB_TYPE,
        schedule_config_type=_RAY_SCHEDULE_CONFIG_TYPE,
        session=session,
    )


__all__ = [
    "RayJobInfo",
    "create_ray_job",
    "delete_ray_job",
    "get_ray_job_detail",
    "list_ray_job_events",
    "list_ray_job_instances",
    "list_ray_job_log_instances",
    "list_ray_job_logs",
    "list_ray_job_scaling_histories",
    "list_ray_jobs",
    "list_ray_offers",
    "stop_ray_job",
]
