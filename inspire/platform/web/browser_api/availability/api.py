"""Browser APIs for compute-group resource identity and availability."""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional
from urllib.parse import quote

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.browser_api.errors import ensure_browser_api_success
from inspire.platform.web.browser_api.v2 import action_url, decode_action_response
from inspire.platform.web import session as web_session_module
from inspire.platform.web.session import (
    DEFAULT_WORKSPACE_ID,
    SessionExpiredError,
    WebSession,
    clear_session_cache,
    get_web_session,
)

from .models import GPUAvailability

logger = logging.getLogger(__name__)


def gpu_type_matches(requested: str | None, *actual: str | None) -> bool:
    """Match a requested class only against platform-reported GPU type fields."""
    target = re.sub(r"[^A-Z0-9]", "", str(requested or "").upper())
    if not target:
        return True
    return any(
        target in re.sub(r"[^A-Z0-9]", "", str(value or "").upper()) for value in actual if value
    )


def list_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List compute groups using the browser API."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page_size": -1,
        "page_num": 1,
        "filter": {"workspace_id": workspace_id},
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/logic_compute_groups/list"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )
    ensure_browser_api_success(data, action="List compute groups")
    return data.get("data", {}).get("logic_compute_groups", [])


def list_compute_pools(
    workspace_id: str,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List physical compute pools and their raw cluster metadata."""
    if session is None:
        session = get_web_session()
    base_url = _get_base_url()
    payload = web_session_module.request_json(
        session,
        "POST",
        action_url(base_url, "cluster", "ListComputeGroups"),
        headers={"Referer": f"{base_url}/jobs/distributedTraining"},
        body={
            "page_size": -1,
            "page_num": 1,
            "filter": {"workspace_id": workspace_id},
        },
        timeout=30,
    )
    result = decode_action_response(payload)
    return result.get("compute_groups", result.get("items", [])) or []


def _dedupe_workspace_ids(workspace_ids: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_workspace_id in workspace_ids:
        workspace_id = str(raw_workspace_id or "").strip()
        if not workspace_id or workspace_id in seen:
            continue
        seen.add(workspace_id)
        ordered.append(workspace_id)
    return ordered


def get_low_priority_gpu_usage(
    *,
    workspace_id: str,
    logic_compute_group_id: str,
    session: Optional[WebSession] = None,
) -> int | None:
    """Read the console's advisory low-priority card count for one group.

    This is the same ``logic_resouces.gpu_low_priority_used`` value the console renders as
    "低优任务占用卡数" (current real-time, for reference only).  It is deliberately separate
    from free capacity: total/used/free now come from per-node dimensions, while this advisory
    observation may still be served by the group resource detail.
    """
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "GET",
        _browser_api_path(
            f"/compute_resources/logic_compute_groups/{logic_compute_group_id}"
            f"?workspace_id={quote(str(workspace_id))}"
        ),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        timeout=30,
    )
    ensure_browser_api_success(data, action="Get low-priority GPU usage")
    resources = (data.get("data") or {}).get("logic_resouces") or {}
    if not isinstance(resources, dict) or "gpu_low_priority_used" not in resources:
        return None
    value = resources.get("gpu_low_priority_used")
    if value is None:
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def get_accurate_gpu_availability(
    workspace_id: Optional[str] = None,
    workspace_ids: Optional[list[str]] = None,
    session: Optional[WebSession] = None,
    workspace_error_callback: Optional[Callable[[str, Exception], None]] = None,
    gpu_type: Optional[str] = None,
    compute_groups: Optional[list[dict]] = None,
    _retry: bool = True,
) -> list[GPUAvailability]:
    """Resolve workspace/group GPU classes and capacity from per-node dimensions."""
    if session is None:
        session = get_web_session()

    if workspace_ids is not None:
        merged: dict[str, GPUAvailability] = {}
        for resolved_workspace_id in _dedupe_workspace_ids(workspace_ids):
            try:
                scoped = get_accurate_gpu_availability(
                    workspace_id=resolved_workspace_id,
                    session=session,
                    gpu_type=gpu_type,
                    compute_groups=compute_groups,
                    _retry=_retry,
                )
            except SessionExpiredError:
                raise
            except Exception as exc:
                if workspace_error_callback is None:
                    raise
                workspace_error_callback(resolved_workspace_id, exc)
                continue
            for item in scoped:
                existing = merged.get(item.group_id)
                if existing is None:
                    merged[item.group_id] = item
                    continue
                if resolved_workspace_id not in existing.workspace_ids:
                    existing.workspace_ids.append(resolved_workspace_id)
        return list(merged.values())

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    requested_gpu_type = gpu_type
    if compute_groups is None:
        try:
            groups = list_compute_groups(workspace_id=workspace_id, session=session)
        except SessionExpiredError:
            if _retry:
                clear_session_cache()
                return get_accurate_gpu_availability(
                    workspace_id=workspace_id,
                    session=None,
                    gpu_type=requested_gpu_type,
                    _retry=False,
                )
            raise
    else:
        groups = list(compute_groups)

    results: list[GPUAvailability] = []
    for group in groups:
        group_id = str(group.get("logic_compute_group_id") or "").strip()
        if not group_id:
            continue
        group_name = str(group.get("name") or group_id)

        try:
            data = web_session_module.fetch_node_dimensions(
                session,
                base_url=_get_base_url(),
                workspace_id=workspace_id,
                logic_compute_group_id=group_id,
            )
        except SessionExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as an unavailable row
            results.append(
                GPUAvailability(
                    group_id=group_id,
                    group_name=group_name,
                    gpu_type="Unknown",
                    total_gpus=0,
                    used_gpus=0,
                    available_gpus=0,
                    low_priority_gpus=None,
                    selection_source="unavailable",
                    fallback_reason=str(exc),
                    workspace_ids=[workspace_id],
                )
            )
            continue

        nodes = list((data.get("data") or {}).get("node_dimensions") or [])
        if not nodes:
            continue

        gpu_total = 0
        gpu_used = 0
        gpu_available = 0
        gpu_per_node = 0
        ready_nodes = 0
        full_free_nodes = 0
        gpu_full_free_nodes = 0
        partial_free_nodes = 0
        display_gpu_type = ""
        canonical_gpu_type = ""

        from inspire.platform.web.resources import node_is_schedulable

        for node in nodes:
            if not node_is_schedulable(node):
                continue
            gpu = node.get("gpu") or {}
            cpu = node.get("cpu") or {}
            memory = node.get("memory") or {}
            node_total = int(gpu.get("total") or 0)
            node_used = int(gpu.get("used") or 0)
            node_available = max(int(gpu.get("available") or 0), 0)
            gpu_total += node_total
            gpu_used += node_used
            gpu_available += node_available
            gpu_per_node = max(gpu_per_node, node_total)
            ready_nodes += 1
            if node_total <= 0:
                continue
            if node_available >= node_total:
                gpu_full_free_nodes += 1
            elif node_available > 0:
                partial_free_nodes += 1
            cpu_total = int(cpu.get("total") or 0)
            cpu_available = int(cpu.get("available") or 0)
            memory_total = float(memory.get("total") or 0)
            memory_available = float(memory.get("available") or 0)
            if (
                node_available >= node_total
                and cpu_available >= cpu_total
                and memory_available >= memory_total
            ):
                full_free_nodes += 1
            gpu_info = node.get("gpu_info") or {}
            display_gpu_type = (
                display_gpu_type or str(gpu_info.get("gpu_type_display") or "").strip()
            )
            canonical_gpu_type = canonical_gpu_type or str(gpu_info.get("gpu_type") or "").strip()

        if not gpu_type_matches(requested_gpu_type, canonical_gpu_type, display_gpu_type):
            continue

        low_priority: int | None = None
        try:
            low_priority = get_low_priority_gpu_usage(
                workspace_id=workspace_id,
                logic_compute_group_id=group_id,
                session=session,
            )
        except Exception:  # noqa: BLE001 - advisory observation, never capacity evidence
            low_priority = None

        results.append(
            GPUAvailability(
                group_id=group_id,
                group_name=group_name,
                gpu_type=display_gpu_type or canonical_gpu_type or "Unknown",
                total_gpus=gpu_total,
                used_gpus=gpu_used,
                available_gpus=gpu_available,
                low_priority_gpus=low_priority,
                low_priority_source=("aggregate_advisory" if low_priority is not None else None),
                free_nodes=full_free_nodes,
                gpu_per_node=gpu_per_node,
                gpu_full_free_nodes=gpu_full_free_nodes,
                schedulable_full_nodes=full_free_nodes,
                partial_free_nodes=partial_free_nodes,
                selection_source="nodes",
                workspace_ids=[workspace_id],
            )
        )

    return results


__all__ = [
    "get_accurate_gpu_availability",
    "gpu_type_matches",
    "list_compute_groups",
    "list_compute_pools",
]
