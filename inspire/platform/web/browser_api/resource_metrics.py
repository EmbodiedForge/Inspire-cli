"""Platform resource metrics helpers for jobs, notebooks, and servings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import time
from collections import Counter
from typing import Any, Optional

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.browser_api.errors import ensure_browser_api_success
from inspire.platform.web.browser_api.inference_servings import (
    get_inference_serving_detail,
    get_inference_serving_versions,
)
from inspire.platform.web.browser_api.hpc_jobs import get_hpc_job_detail
from inspire.platform.web.browser_api.jobs import list_task_dimensions
from inspire.platform.web.browser_api.notebooks import get_notebook_detail
from inspire.platform.web.browser_api.ray_jobs import get_ray_job_detail
from inspire.platform.web.session import WebSession, get_web_session

METRIC_TYPES = [
    "gpu_usage_rate",
    "gpu_memory_usage_rate",
    "cpu_usage_rate",
    "memory_usage_rate",
    "disk_io_read",
    "disk_io_write",
    "network_io_read",
    "network_io_write",
    "network_storage_io_read",
    "network_storage_io_write",
]
DEFAULT_METRIC_TYPES = ["gpu_usage_rate", "gpu_memory_usage_rate"]
#: What a notebook without a GPU can still answer.  The console's notebook monitoring panel
#: deletes every GPU chart when the notebook has none - `assets/InteractiveDetail.B9Bbw0_y.js`
#: offset 45128 is `const m=!((T=t.start_config)!=null&&T.gpu_count)`, and offset 46180 then
#: starts `N.delete("gpu_usage_rate")`.  Measured on a 40 CPU / 400 GiB notebook on
#: 2026-11-06: `gpu_usage_rate` answers an empty `time_seris_metric_groups`, while
#: `cpu_usage_rate` and `memory_usage_rate` answer points.
CPU_METRIC_TYPES = ["cpu_usage_rate", "memory_usage_rate"]

#: The window each sample interval may span, as the console itself pairs them.  Both functions are
#: in `assets/Search.DLufsIGr.js`; the first is the interval picker's only legal choice for a
#: range (offset 3151881), the second clamps the range itself (offset 3152665, `f7=30` at 3152659):
#:
#:     gIe=(s,e)=>{const{exceedsFiveHours:t,exceedsTwentyFiveHours:i,exceedsSixDays:n}=cye(s,e);
#:                 return n?[60]:i?[30]:t?[5]:[1]}
#:     mIe=(s,e)=>e.diff(s,"day",!0)>f7?[e.subtract(f7,"day"),e]:[s,e]
#:
#: Minutes there, seconds here: 1 min up to 5 h, 5 min up to 25 h, 30 min up to 6 days, 1 hour
#: beyond, and no range over 30 days.
#:
#: The platform enforces both halves of that pairing (measured 2026-11-06 against
#: `notebook?Action=GetTaskMetric` on a live notebook): an interval outside the four answers
#: `InternalError: 时间间隔仅支持 1 分钟 / 5 分钟 / 30 分钟 / 1 小时`, and one too fine for the window
#: answers `InternalError: 当前时间区间下，时间间隔过小` - 24 h at 60 s and 3 d at 300 s were both
#: refused that way, while 7 h at 60 s and 30 h at 300 s answered with data.  So every pair here
#: is one the live Action accepts, and a longer window than the pair is one it does not.
NOTEBOOK_METRIC_INTERVAL_WINDOWS: dict[int, int] = {
    60: 5 * 3600,
    300: 25 * 3600,
    1800: 6 * 86400,
    3600: 30 * 86400,
}

_TASK_TYPES = {
    "hpc": ("hpc_job",),
    "notebook": ("interactive_modeling",),
    "ray": ("ray_job",),
    "serving": ("inference_serving_customize", "inference_serving"),
}
_TERMINAL_STATUSES = {
    "hpc": {"FAILED", "STOPPED", "SUCCEEDED", "COMPLETED", "DELETED"},
    "notebook": {"FAILED", "ERROR", "STOPPED", "notebook_failed", "notebook_stopped"},
    "ray": {"FAILED", "STOPPED", "SUCCEEDED", "COMPLETED", "DELETED"},
    "serving": {"FAILED", "STOPPED"},
}


@dataclass(frozen=True)
class ResourceMetricContext:
    resource_type: str
    resource_id: str
    name: str
    workspace_id: str
    logic_compute_group_id: str
    referer_path: str
    task_types: tuple[str, ...]
    start: int
    end: int
    selection: dict[str, Any] | None = None
    gpu_count: int | None = None
    #: Set when the requested interval cannot span the resource's whole window, so the caller can
    #: be told the answer covers less than the resource's life instead of quietly reading it as so.
    window_clamped: dict[str, Any] | None = None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _int_value(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _timestamp_seconds(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return int(numeric / 1000) if numeric > 10_000_000_000 else int(numeric)
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
        return int(numeric / 1000) if numeric > 10_000_000_000 else int(numeric)
    except ValueError:
        pass
    try:
        normalized = text.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return None


def _webui_timestamp_seconds(value: Any) -> int | None:
    return _timestamp_seconds(value)


def _resource_metric_end_timestamp(resource_type: str, detail: dict[str, Any]) -> int:
    status = str(detail.get("status") or "")
    if status in _TERMINAL_STATUSES[resource_type]:
        for field in ("finished_at", "updated_time", "updated_at"):
            timestamp = _timestamp_seconds(detail.get(field))
            if timestamp:
                return timestamp
    return int(time.time())


def list_notebook_run_indexes(
    notebook_id: str,
    *,
    workspace_id: str = "",
    session: Optional[WebSession] = None,
) -> list[dict[str, Any]]:
    """List notebook run indexes used to narrow metrics windows."""
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/run_index/list"),
        referer=f"{_get_base_url()}/interactiveModelDetail/{notebook_id}",
        body={"notebook_id": notebook_id},
        timeout=30,
    )
    ensure_browser_api_success(data, action="List notebook run indexes")
    payload = data.get("data", {})
    items = payload.get("list", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _run_index_sort_key(run: dict[str, Any]) -> tuple[int, int, int]:
    index = _int_value(run.get("index")) or -1
    started = _webui_timestamp_seconds(run.get("start_time")) or 0
    ended = _webui_timestamp_seconds(run.get("end_time")) or 0
    return (index, started, ended)


def _select_notebook_run(
    runs: list[dict[str, Any]],
    requested_run_index: int | None,
) -> dict[str, Any]:
    if requested_run_index in (None, -1):
        return max(runs, key=_run_index_sort_key)
    for run in runs:
        if _int_value(run.get("index")) == requested_run_index:
            return run
    raise ValueError(f"Notebook run index {requested_run_index} not found")


def _serving_versions_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("inference_servings", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _serving_version_sort_key(item: dict[str, Any]) -> tuple[int, int, int]:
    created = _timestamp_seconds(item.get("created_time") or item.get("created_at")) or 0
    version = _int_value(item.get("version")) or -1
    updated = _timestamp_seconds(item.get("updated_time") or item.get("updated_at")) or 0
    return (created, version, updated)


def _select_serving_version(
    versions: list[dict[str, Any]],
    requested_version_index: int | None,
) -> tuple[int, dict[str, Any]]:
    ordered = sorted(versions, key=_serving_version_sort_key)
    if requested_version_index is not None and requested_version_index != -1:
        if 0 <= requested_version_index < len(ordered):
            return requested_version_index, ordered[requested_version_index]
        raise ValueError(f"Serving version index {requested_version_index} not found")
    active = [(idx, item) for idx, item in enumerate(ordered) if item.get("is_active")]
    if active:
        return max(active, key=lambda pair: _serving_version_sort_key(pair[1]))
    return max(
        ((idx, item) for idx, item in enumerate(ordered)),
        key=lambda pair: _serving_version_sort_key(pair[1]),
    )


def _notebook_metric_context(
    notebook_id: str,
    *,
    run_index: int | None,
    session: WebSession,
) -> ResourceMetricContext:
    detail = get_notebook_detail(notebook_id=notebook_id, session=session)
    resolved_id = str(_first_present(detail.get("notebook_id"), detail.get("id"), notebook_id))
    workspace_id = str(
        _first_present(
            detail.get("workspace_id"),
            (
                (detail.get("workspace") or {}).get("id")
                if isinstance(detail.get("workspace"), dict)
                else None
            ),
        )
        or ""
    )
    logic_compute_group_id = str(
        _first_present(
            detail.get("logic_compute_group_id"),
            (
                (detail.get("start_config") or {}).get("logic_compute_group_id")
                if isinstance(detail.get("start_config"), dict)
                else None
            ),
            (
                (detail.get("logic_compute_group") or {}).get("id")
                if isinstance(detail.get("logic_compute_group"), dict)
                else None
            ),
        )
        or ""
    )
    start = _timestamp_seconds(detail.get("created_at")) or 0
    end = _resource_metric_end_timestamp("notebook", detail)
    selection: dict[str, Any] | None = None
    start_config = detail.get("start_config")
    if not isinstance(start_config, dict):
        start_config = {}
    gpu_count = _int_value(_first_present(detail.get("gpu_count"), start_config.get("gpu_count")))

    runs = list_notebook_run_indexes(resolved_id, workspace_id=workspace_id, session=session)
    if runs:
        selected_run = _select_notebook_run(runs, run_index)
        run_start = _webui_timestamp_seconds(selected_run.get("start_time"))
        run_end = _webui_timestamp_seconds(selected_run.get("end_time"))
        if run_start is not None:
            start = run_start
        if run_end is not None:
            end = run_end
        if end < start:
            end = start
        selection = {"kind": "run"}
        selected_run_index = _int_value(selected_run.get("index"))
        if selected_run_index is not None:
            selection["run_index"] = selected_run_index
    elif run_index not in (None, -1):
        raise ValueError(f"Notebook run index {run_index} not found for {resolved_id}")

    return ResourceMetricContext(
        resource_type="notebook",
        resource_id=resolved_id,
        name=str(detail.get("name") or ""),
        workspace_id=workspace_id,
        logic_compute_group_id=logic_compute_group_id,
        referer_path=f"interactiveModelDetail/{resolved_id}",
        task_types=_TASK_TYPES["notebook"],
        start=start,
        end=end,
        selection=selection,
        gpu_count=gpu_count,
    )


def default_notebook_metric_types(context: ResourceMetricContext) -> list[str]:
    """Metric types to fetch when the caller named none (see :data:`CPU_METRIC_TYPES`)."""
    return list(DEFAULT_METRIC_TYPES) if context.gpu_count else list(CPU_METRIC_TYPES)


def _fit_notebook_metric_window(
    context: ResourceMetricContext,
    interval_seconds: int,
) -> ResourceMetricContext:
    """Narrow the window to what ``interval_seconds`` may legally span.

    The platform pairs a sample interval with a maximum window (see
    :data:`NOTEBOOK_METRIC_INTERVAL_WINDOWS`), and it answers that pairing as an
    ``InternalError`` rather than as a truncation.  Keeping the interval the caller asked for and
    moving ``start`` forward is the reading that still returns data - the newest samples at the
    resolution that was asked for - and the clamp is reported in the result, so a five-hour answer
    is never mistaken for the life of a notebook that ran longer.
    """
    max_span = NOTEBOOK_METRIC_INTERVAL_WINDOWS.get(interval_seconds)
    if max_span is None:
        allowed = ", ".join(str(value) for value in sorted(NOTEBOOK_METRIC_INTERVAL_WINDOWS))
        raise ValueError(
            f"Notebook metrics interval must be one of {allowed} seconds; the platform answers "
            "'时间间隔仅支持 1 分钟 / 5 分钟 / 30 分钟 / 1 小时' (only 1 / 5 / 30 minutes and 1 hour "
            "are supported) for anything else"
        )
    if context.end - context.start <= max_span:
        return context
    return replace(
        context,
        start=context.end - max_span,
        window_clamped={
            "interval_seconds": interval_seconds,
            "max_span_seconds": max_span,
            "requested_start": context.start,
        },
    )


def _serving_metric_context(
    serving_id: str,
    *,
    version_index: int | None,
    session: WebSession,
) -> ResourceMetricContext:
    detail = get_inference_serving_detail(serving_id=serving_id, session=session)
    resolved_id = detail.serving_id or serving_id
    start = _timestamp_seconds(detail.created_at) or 0
    end = _resource_metric_end_timestamp("serving", {"status": detail.status})
    selection = None

    versions_payload = get_inference_serving_versions(resolved_id, session=session)
    versions = _serving_versions_list(versions_payload)
    if versions:
        selected_index, selected_version = _select_serving_version(versions, version_index)
        version_start = _timestamp_seconds(
            selected_version.get("created_time") or selected_version.get("created_at")
        )
        if version_start is not None:
            start = version_start
        if not selected_version.get("is_active"):
            version_end = _timestamp_seconds(
                selected_version.get("updated_time") or selected_version.get("updated_at")
            )
            if version_end is not None:
                end = version_end
        if end < start:
            end = start
        selection = {"kind": "version", "version": selected_index}
    elif version_index not in (None, -1):
        raise ValueError(f"Serving version index {version_index} not found for {resolved_id}")

    return ResourceMetricContext(
        resource_type="serving",
        resource_id=resolved_id,
        name=detail.name,
        workspace_id=detail.workspace_id,
        logic_compute_group_id=str(getattr(detail, "logic_compute_group_id", "") or ""),
        referer_path=f"modelDeplayDetail/{resolved_id}",
        task_types=_TASK_TYPES["serving"],
        start=start,
        end=end,
        selection=selection,
    )


def _workload_metric_context(
    kind: str,
    resource_id: str,
    *,
    session: WebSession,
) -> tuple[ResourceMetricContext, str]:
    detail = (
        get_hpc_job_detail(resource_id, session=session)
        if kind == "hpc"
        else get_ray_job_detail(resource_id, session=session)
    )
    resolved_id = str(
        _first_present(
            detail.get("job_id"),
            detail.get("ray_job_id"),
            detail.get("id"),
            resource_id,
        )
    )
    cluster_spec = detail.get("slurm_cluster_spec")
    if not isinstance(cluster_spec, dict):
        cluster_spec = {}
    head_node = detail.get("head_node")
    if not isinstance(head_node, dict):
        head_node = {}
    logic_compute_group_id = str(
        _first_present(
            detail.get("logic_compute_group_id"),
            cluster_spec.get("logic_compute_group_id"),
            (
                (cluster_spec.get("spec_price") or {}).get("logic_compute_group_id")
                if isinstance(cluster_spec.get("spec_price"), dict)
                else None
            ),
            head_node.get("logic_compute_group_id"),
        )
        or ""
    )
    start = (
        _timestamp_seconds(_first_present(detail.get("created_at"), detail.get("started_at"))) or 0
    )
    end = _resource_metric_end_timestamp(kind, detail)
    if end < start:
        end = start
    project_id = str(detail.get("project_id") or "")
    return (
        ResourceMetricContext(
            resource_type=kind,
            resource_id=resolved_id,
            name=str(detail.get("name") or detail.get("job_name") or ""),
            workspace_id=str(detail.get("workspace_id") or ""),
            logic_compute_group_id=logic_compute_group_id,
            referer_path=(f"jobs/hpcDetail/{resolved_id}" if kind == "hpc" else "jobs/ray"),
            task_types=_TASK_TYPES[kind],
            start=start,
            end=end,
        ),
        project_id,
    )


def _task_dimension_ids(task: dict[str, Any]) -> set[str]:
    ids = set()
    for key in (
        "id",
        "task_id",
        "job_id",
        "hpc_job_id",
        "ray_job_id",
        "notebook_id",
        "inference_serving_id",
        "serving_id",
    ):
        value = task.get(key)
        if value not in (None, ""):
            ids.add(str(value))
    return ids


def _task_dimension_debug_summary(payload: dict[str, Any]) -> dict[str, Any]:
    raw_dimensions = payload.get("task_dimensions", []) if isinstance(payload, dict) else []
    dimensions = [item for item in raw_dimensions if isinstance(item, dict)]
    type_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    id_field_counts: Counter[str] = Counter()
    name_present = 0
    id_fields = (
        "id",
        "task_id",
        "job_id",
        "hpc_job_id",
        "ray_job_id",
        "notebook_id",
        "inference_serving_id",
        "serving_id",
    )

    for task in dimensions:
        type_counts[str(task.get("type") or "<missing>")] += 1
        status_counts[str(task.get("status") or "<missing>")] += 1
        present = tuple(key for key in id_fields if task.get(key) not in (None, ""))
        id_field_counts[",".join(present) if present else "<none>"] += 1
        if task.get("name") not in (None, ""):
            name_present += 1

    return {
        "total": payload.get("total") if isinstance(payload, dict) else None,
        "returned": len(dimensions),
        "types": dict(type_counts.most_common(10)),
        "statuses": dict(status_counts.most_common(10)),
        "id_field_sets": dict(id_field_counts.most_common(10)),
        "name_present": name_present,
    }


def _iter_task_dimensions(
    *,
    workspace_id: str,
    project_id: str = "",
    session: WebSession,
    page_size: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect every task dimension page.

    A short page proves nothing here: the `cluster_metric` endpoints answer with fewer rows than
    `page_size` even when more pages exist, so a cap below the request truncated this to page one
    and every metrics command on a dimensioned resource then failed to find its dimension. Stop at
    the advertised `total` or an empty page, like `fetch_node_dimensions` does.
    """
    items: list[dict[str, Any]] = []
    page_num = 1
    total = 0
    last_payload: dict[str, Any] = {}
    while True:
        payload = list_task_dimensions(
            workspace_id=workspace_id,
            project_id=project_id,
            page_num=page_num,
            page_size=page_size,
            session=session,
        )
        last_payload = payload
        page_items = payload.get("task_dimensions", []) if isinstance(payload, dict) else []
        if not isinstance(page_items, list) or not page_items:
            return items, last_payload
        before = len(items)
        items.extend(item for item in page_items if isinstance(item, dict))
        try:
            total = int(payload.get("total", 0) or 0)
        except (TypeError, ValueError):
            total = 0
        if total and len(items) >= total:
            return items, last_payload
        if len(items) == before:
            return items, last_payload
        if not total and len(page_items) < page_size:
            # Without an advertised `total` a short page is the only end-of-pages signal left.
            return items, last_payload
        page_num += 1


def _resolve_metric_task_dimension(
    context: ResourceMetricContext,
    *,
    project_id: str = "",
    session: WebSession,
) -> dict[str, Any]:
    if not context.workspace_id:
        raise ValueError(
            f"{context.resource_type} {context.resource_id} detail did not include workspace_id"
        )
    dimensions, last_payload = _iter_task_dimensions(
        workspace_id=context.workspace_id,
        project_id=project_id,
        session=session,
    )
    expected_types = set(context.task_types)
    name_matches: list[dict[str, Any]] = []
    for task in dimensions:
        task_type = str(task.get("type") or "")
        if task_type and expected_types and task_type not in expected_types:
            continue
        if context.resource_id in _task_dimension_ids(task):
            return task
        if context.name and str(task.get("name") or "") == context.name:
            name_matches.append(task)
    if len(name_matches) == 1:
        return name_matches[0]
    summary = _task_dimension_debug_summary(last_payload)
    raise ValueError(
        f"Resource metrics task dimension not found for {context.resource_type} "
        f"{context.resource_id}; platform task_dimensions summary: {summary}"
    )


def _logic_group_from_dimension(task_dimension: dict[str, Any], fallback: str) -> str:
    return str(
        _first_present(
            task_dimension.get("logic_compute_group_id"),
            task_dimension.get("logicComputeGroupId"),
            (
                (task_dimension.get("logic_compute_group") or {}).get("id")
                if isinstance(task_dimension.get("logic_compute_group"), dict)
                else None
            ),
            fallback,
        )
        or ""
    )


def _fetch_metric_groups(
    context: ResourceMetricContext,
    *,
    task_type: str,
    metric_type: str,
    interval_seconds: int,
    session: WebSession,
) -> dict[str, Any]:
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/cluster_metric/resource_metric_by_time"),
        referer=f"{_get_base_url()}/{context.referer_path}",
        body={
            "metric_types": [metric_type],
            "filter": {
                "logic_compute_group_id": context.logic_compute_group_id,
                "task_type": task_type,
                "task_id": context.resource_id,
            },
            "time_range": {
                "start_timestamp": context.start,
                "end_timestamp": context.end,
                "interval_second": interval_seconds,
            },
        },
        timeout=30,
    )
    ensure_browser_api_success(data, action="Get resource metrics")
    payload = data.get("data", {})
    return payload if isinstance(payload, dict) else {}


def _resource_metrics(
    context: ResourceMetricContext,
    *,
    metric_types: list[str] | None,
    interval_seconds: int,
    raw: bool,
    project_id: str = "",
    session: WebSession,
    resolve_dimension: bool = True,
) -> dict[str, Any]:
    """Fetch every requested metric type for one resource.

    ``resolve_dimension`` says where the compute group and task type come from.  Jobs and
    servings read them out of ``workspace?Action=ListTaskDimension``; a notebook does not go there
    at all, because its own detail already carries both - see
    :func:`get_notebook_resource_metrics`.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    selected_metric_types = metric_types or DEFAULT_METRIC_TYPES
    if not resolve_dimension:
        # The console's notebook page never asks for a task dimension: the compute group comes off
        # the detail and the task type is a literal.  Quoted from `assets/InteractiveDetail.
        # B9Bbw0_y.js` offset 47330 (`Bt` is the metrics fetcher it imports):
        #     Bt({metricTypes:[m],clusterId:(r=t==null?void 0:t.start_config)
        # and, at offset 47467:
        #     timeRange:L.current,taskType:"interactive_modeling"
        # That reading is why this path does not depend on a task-dimension lookup: a
        # `workspace?Action=ListTaskDimension` row carries no compute group, and the console's
        # notebook page never asked it for the task type either.
        task_type = context.task_types[0]
        if not context.logic_compute_group_id:
            raise ValueError(
                f"{context.resource_type} {context.resource_id} detail did not include "
                "logic_compute_group_id"
            )
    else:
        task_dimension = _resolve_metric_task_dimension(
            context, project_id=project_id, session=session
        )
        task_type = str(task_dimension.get("type") or context.task_types[0])
        logic_compute_group_id = _logic_group_from_dimension(
            task_dimension,
            context.logic_compute_group_id,
        )
        if not logic_compute_group_id:
            raise ValueError(
                f"Task dimension for {context.resource_type} {context.resource_id} did not include "
                "logic_compute_group_id"
            )
        context = replace(context, logic_compute_group_id=logic_compute_group_id)

    metrics: dict[str, Any] = {}
    for metric_type in selected_metric_types:
        payload = _fetch_metric_groups(
            context,
            task_type=task_type,
            metric_type=metric_type,
            interval_seconds=interval_seconds,
            session=session,
        )
        metrics[metric_type] = payload if raw else payload.get("time_seris_metric_groups", [])

    result = {
        "resource_type": context.resource_type,
        "resource_id": context.resource_id,
        "workspace_id": context.workspace_id,
        "logic_compute_group_id": context.logic_compute_group_id,
        "task_type": task_type,
        "interval_seconds": interval_seconds,
        "metric_types": selected_metric_types,
        "start": context.start,
        "end": context.end,
        "metrics": metrics,
    }
    if context.selection:
        result["selection"] = context.selection
    if context.window_clamped:
        result["window_clamped"] = context.window_clamped
    return result


def _narrow_metric_window(
    context: ResourceMetricContext,
    since_seconds: int,
) -> ResourceMetricContext:
    """Keep the newest ``since_seconds`` of the resource's life.

    A caller that asked for six hours must not be handed the resource's whole life, and the
    interval's maximum span must not be passed off as the window that was asked for. Narrowing
    runs before :func:`_fit_notebook_metric_window`, so a window the sample interval cannot span
    is still reported as clamped - against the window that was asked for, not against the life of
    the resource.
    """
    if since_seconds <= 0:
        raise ValueError("Metric window must be longer than zero seconds")
    earliest = context.end - since_seconds
    if context.end <= 0 or context.start >= earliest:
        return context
    return replace(context, start=earliest)


def get_notebook_resource_metrics(
    notebook_id: str,
    *,
    metric_types: list[str] | None = None,
    interval_seconds: int = 300,
    run_index: int | None = None,
    raw: bool = False,
    since_seconds: int | None = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch platform historical resource metrics for a notebook.

    Three things here are the console's own reading rather than a choice of ours, and each is
    measured against the live platform on 2026-11-06:

    * the sample interval decides how wide a window the Action accepts, so the window is fitted to
      the interval (:func:`_fit_notebook_metric_window`) instead of being refused;
    * a notebook with no GPU has no GPU series to fetch (:func:`default_notebook_metric_types`);
    * the compute group and task type come from the notebook detail, not from a task dimension
      (:func:`_resource_metrics` with ``resolve_dimension=False``).

    ``since_seconds``, when given, narrows the resource's life to that window first
    (:func:`_narrow_metric_window`).
    """
    if session is None:
        session = get_web_session()
    context = _notebook_metric_context(notebook_id, run_index=run_index, session=session)
    if since_seconds is not None:
        context = _narrow_metric_window(context, since_seconds)
    context = _fit_notebook_metric_window(context, interval_seconds)
    return _resource_metrics(
        context,
        metric_types=metric_types or default_notebook_metric_types(context),
        interval_seconds=interval_seconds,
        raw=raw,
        session=session,
        resolve_dimension=False,
    )


def get_inference_serving_resource_metrics(
    serving_id: str,
    *,
    metric_types: list[str] | None = None,
    interval_seconds: int = 300,
    version_index: int | None = None,
    raw: bool = False,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch platform historical resource metrics for a model serving."""
    if session is None:
        session = get_web_session()
    context = _serving_metric_context(serving_id, version_index=version_index, session=session)
    return _resource_metrics(
        context,
        metric_types=metric_types,
        interval_seconds=interval_seconds,
        raw=raw,
        session=session,
    )


def get_hpc_job_resource_metrics(
    job_id: str,
    *,
    metric_types: list[str] | None = None,
    interval_seconds: int = 300,
    raw: bool = False,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch platform historical resource metrics for an HPC job."""
    if session is None:
        session = get_web_session()
    context, project_id = _workload_metric_context("hpc", job_id, session=session)
    return _resource_metrics(
        context,
        metric_types=metric_types,
        interval_seconds=interval_seconds,
        raw=raw,
        project_id=project_id,
        session=session,
    )


def get_ray_job_resource_metrics(
    ray_job_id: str,
    *,
    metric_types: list[str] | None = None,
    interval_seconds: int = 300,
    raw: bool = False,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch platform historical resource metrics for a Ray job."""
    if session is None:
        session = get_web_session()
    context, project_id = _workload_metric_context("ray", ray_job_id, session=session)
    return _resource_metrics(
        context,
        metric_types=metric_types,
        interval_seconds=interval_seconds,
        raw=raw,
        project_id=project_id,
        session=session,
    )


__all__ = [
    "CPU_METRIC_TYPES",
    "DEFAULT_METRIC_TYPES",
    "METRIC_TYPES",
    "NOTEBOOK_METRIC_INTERVAL_WINDOWS",
    "default_notebook_metric_types",
    "get_hpc_job_resource_metrics",
    "get_inference_serving_resource_metrics",
    "get_notebook_resource_metrics",
    "get_ray_job_resource_metrics",
    "list_notebook_run_indexes",
]
