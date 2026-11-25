"""Browser API adapter for platform HPC jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

_HPC_JOB_TYPE = "hpc_job"
_HPC_SCHEDULE_CONFIG_TYPE = "SCHEDULE_CONFIG_TYPE_HPC"


@dataclass(frozen=True)
class HPCJobInfo:
    """Summary row returned by the platform HPC list action."""

    job_id: str
    name: str
    status: str
    workspace_id: str
    project_id: str
    project_name: str
    compute_group_name: str
    created_at: str
    finished_at: str | None
    created_by_id: str
    created_by_name: str
    entrypoint: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "HPCJobInfo":
        created_by = data.get("created_by")
        if not isinstance(created_by, dict):
            created_by = {}
        return cls(
            job_id=str(data.get("job_id") or data.get("id") or ""),
            name=str(data.get("name") or data.get("job_name") or ""),
            status=str(data.get("status") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            project_id=str(data.get("project_id") or ""),
            project_name=str(data.get("project_name") or ""),
            compute_group_name=str(data.get("logic_compute_group_name") or ""),
            created_at=str(data.get("created_at") or ""),
            finished_at=str(data.get("finished_at")) if data.get("finished_at") else None,
            created_by_id=str(created_by.get("id") or data.get("created_by_id") or ""),
            created_by_name=str(created_by.get("name") or data.get("created_by_name") or ""),
            entrypoint=str(data.get("entrypoint") or data.get("command") or ""),
            raw=dict(data),
        )


def _hpc_referer(job_id: str | None = None) -> str:
    if job_id:
        return f"{_get_base_url()}/jobs/hpcDetail/{job_id}"
    return f"{_get_base_url()}/jobs/highPerformanceComputing"


def _as_total(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def list_hpc_jobs(
    *,
    workspace_id: str,
    created_by: str | None = None,
    status: str | None = None,
    page_num: int = 1,
    page_size: int = 50,
    session: WebSession | None = None,
) -> tuple[list[HPCJobInfo], int]:
    """List HPC jobs via the platform's current v2 ListJobs action."""
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
    if created_by:
        body["created_by"] = created_by
    if status:
        body["status"] = status

    response = _request_json(
        session,
        "POST",
        "/api/v2/hpc?Action=ListJobs",
        referer=_hpc_referer(),
        body=body,
        timeout=30,
    )
    result = get_browser_api_action_result(response, action="List HPC jobs")
    raw_jobs = result.get("jobs", [])
    if not isinstance(raw_jobs, list):
        raise ValueError("List HPC jobs failed: platform jobs is not a list")
    jobs = [HPCJobInfo.from_api_response(item) for item in raw_jobs if isinstance(item, dict)]
    return jobs, _as_total(result.get("total"), len(jobs))


def get_hpc_job_detail(
    job_id: str,
    *,
    session: WebSession | None = None,
) -> dict[str, Any]:
    """Fetch one HPC job via the platform's current v2 GetJob action."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        "/api/v2/hpc?Action=GetJob",
        referer=_hpc_referer(job_id),
        body={"job_id": job_id},
        timeout=30,
    )
    return get_browser_api_action_result(response, action="Get HPC job")


def create_hpc_job(
    payload: dict[str, Any],
    *,
    session: WebSession | None = None,
) -> dict[str, Any]:
    """Create an HPC job with an already prepared native platform payload."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload must be a non-empty dict")
    if session is None:
        session = get_web_session()
    response = _request_json(
        session,
        "POST",
        "/api/v2/hpc?Action=CreateJobConsole",
        referer=_hpc_referer(),
        body=payload,
        timeout=60,
    )
    return get_browser_api_action_result(response, action="Create HPC job")


def stop_hpc_job(
    job_id: str,
    *,
    session: WebSession | None = None,
) -> dict[str, Any]:
    """Stop a running HPC job."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    if session is None:
        session = get_web_session()
    response = _request_json(
        session,
        "POST",
        "/api/v2/hpc?Action=StopJob",
        referer=_hpc_referer(job_id),
        body={"job_id": job_id},
        timeout=30,
    )
    return get_browser_api_action_result(response, action="Stop HPC job")


def delete_hpc_job(
    job_id: str,
    *,
    session: WebSession | None = None,
) -> dict[str, Any]:
    """Delete an HPC job record via the platform's REST endpoint."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    if session is None:
        session = get_web_session()
    response = _request_json(
        session,
        "DELETE",
        _browser_api_path(f"/hpc_jobs/{job_id}"),
        referer=_hpc_referer(job_id),
        timeout=30,
    )
    ensure_browser_api_success(response, action="Delete HPC job")
    payload = response.get("data")
    return payload if isinstance(payload, dict) else {}


def list_hpc_job_events(
    job_id: str,
    *,
    session: WebSession | None = None,
) -> list[dict[str, Any]]:
    """List job-level scheduler events for an HPC job."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        _browser_api_path("/hpc_jobs/events/list"),
        referer=_hpc_referer(job_id),
        body={
            "pageNum": -1,
            "pageSize": 200,
            "filter": {"object_ids": [job_id], "object_type": "HPC_JOB"},
            "sorter": [{"field": "last_timestamp", "sort": "ascend"}],
        },
        timeout=30,
    )
    if (
        response.get("code") == 100000
        and "record not found" in str(response.get("message") or "").lower()
    ):
        return []
    ensure_browser_api_success(response, action="List HPC job events")
    payload = response.get("data")
    if not isinstance(payload, dict):
        return []
    for key in ("events", "items", "list"):
        items = payload.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def list_hpc_job_instances(
    job_id: str,
    *,
    page_num: int = 1,
    page_size: int = 100,
    session: WebSession | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """List component/pod instances for an HPC job."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        _browser_api_path("/hpc_jobs/instances/list"),
        referer=_hpc_referer(job_id),
        body={"jobId": job_id, "page_num": page_num, "page_size": page_size},
        timeout=30,
    )
    ensure_browser_api_success(response, action="List HPC job instances")
    payload = response.get("data")
    if not isinstance(payload, dict):
        return [], 0
    raw_items = payload.get("items", payload.get("list", []))
    if not isinstance(raw_items, list):
        raise ValueError("List HPC job instances failed: platform items is not a list")
    items = [item for item in raw_items if isinstance(item, dict)]
    return items, _as_total(payload.get("total"), len(items))


def list_hpc_job_logs(
    *,
    pod_names: list[str],
    start_timestamp_ms: int | str,
    end_timestamp_ms: int | str,
    page_size: int = 200,
    job_id: str | None = None,
    session: WebSession | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch aggregated HPC logs for exact pod names."""
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
        _browser_api_path("/logs/hpc"),
        referer=_hpc_referer(job_id),
        body={
            # The console's log viewer sends job_id on the hpc branch (and only there); the
            # pod names identify the job too, but the platform rejects a job id that does not
            # own the named instances, so sending the one we were given matches its caller.
            **({"job_id": str(job_id)} if job_id else {}),
            "page_size": page_size,
            "filter": {
                "podNames": names,
                "start_timestamp_ms": str(start_timestamp_ms),
                "end_timestamp_ms": str(end_timestamp_ms),
            },
        },
        timeout=30,
    )
    ensure_browser_api_success(response, action="List HPC job logs")
    payload = response.get("data")
    if not isinstance(payload, dict):
        return [], 0
    raw_items = payload.get("logs", payload.get("items", []))
    if not isinstance(raw_items, list):
        raise ValueError("List HPC job logs failed: platform logs is not a list")
    items = [item for item in raw_items if isinstance(item, dict)]
    return items, _as_total(payload.get("total"), len(items))


def list_hpc_offers(
    *,
    workspace_id: str,
    session: WebSession | None = None,
) -> WorkloadOfferDiscovery:
    """Resolve launchable HPC offers from live capability and quota contracts."""
    return discover_workload_offers(
        workload="hpc",
        workspace_id=workspace_id,
        platform_job_type=_HPC_JOB_TYPE,
        schedule_config_type=_HPC_SCHEDULE_CONFIG_TYPE,
        session=session,
    )


__all__ = [
    "HPCJobInfo",
    "create_hpc_job",
    "delete_hpc_job",
    "get_hpc_job_detail",
    "list_hpc_job_events",
    "list_hpc_job_instances",
    "list_hpc_job_logs",
    "list_hpc_jobs",
    "list_hpc_offers",
    "stop_hpc_job",
]
