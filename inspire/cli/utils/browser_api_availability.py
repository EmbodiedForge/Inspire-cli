"""Browser (web-session) APIs for compute group availability and selection.

The web UI exposes endpoints for:
- aggregated GPU usage per compute group
- per-node (fragmentation-aware) "full free node" availability

These endpoints require a web-session cookie and are not part of the OpenAPI surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from inspire.cli.utils.browser_api_core import BASE_URL, _browser_api_path, _request_json
from inspire.cli.utils.web_session import (
    DEFAULT_WORKSPACE_ID,
    SessionExpiredError,
    WebSession,
    clear_session_cache,
    get_web_session,
)

__all__ = [
    "FullFreeNodeCount",
    "GPUAvailability",
    "find_best_compute_group_accurate",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "list_compute_groups",
]


@dataclass
class GPUAvailability:
    """GPU availability for a compute group."""

    group_id: str
    group_name: str
    gpu_type: str
    total_gpus: int
    used_gpus: int
    available_gpus: int
    low_priority_gpus: int  # GPUs used by low-priority tasks (can be preempted)
    free_nodes: int = 0
    gpu_per_node: int = 0
    selection_source: str = "aggregate"


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
        referer=f"{BASE_URL}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )
    return data.get("data", {}).get("logic_compute_groups", [])


def get_accurate_gpu_availability(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
    _retry: bool = True,
) -> list[GPUAvailability]:
    """Get accurate GPU availability for all compute groups."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    try:
        groups = list_compute_groups(workspace_id=workspace_id, session=session)
    except SessionExpiredError:
        if _retry:
            clear_session_cache()
            return get_accurate_gpu_availability(
                workspace_id=workspace_id,
                session=None,
                _retry=False,
            )
        raise

    results: list[GPUAvailability] = []

    for group in groups:
        group_id = group["logic_compute_group_id"]
        group_name = group["name"]

        try:
            data = _request_json(
                session,
                "GET",
                _browser_api_path(f"/compute_resources/logic_compute_groups/{group_id}"),
                referer=f"{BASE_URL}/jobs/distributedTraining",
                timeout=30,
            )
        except SessionExpiredError:
            raise
        except ValueError:
            continue

        resources = data.get("data", {}).get("logic_resouces", {})
        gpu_stats = data.get("data", {}).get("gpu_type_stats", [{}])

        gpu_type = ""
        if gpu_stats:
            gpu_type = gpu_stats[0].get("gpu_info", {}).get("gpu_type_display", "Unknown")

        gpu_total = resources.get("gpu_total", 0)
        gpu_used = resources.get("gpu_used", 0)
        gpu_low_priority = resources.get("gpu_low_priority_used", 0)
        gpu_available = gpu_total - gpu_used

        results.append(
            GPUAvailability(
                group_id=group_id,
                group_name=group_name,
                gpu_type=gpu_type,
                total_gpus=gpu_total,
                used_gpus=gpu_used,
                available_gpus=gpu_available,
                low_priority_gpus=gpu_low_priority,
            )
        )

    return results


def find_best_compute_group_accurate(
    gpu_type: Optional[str] = None,
    min_gpus: int = 8,
    preferred_groups: Optional[list[str]] = None,
    include_preemptible: bool = True,
    instance_count: int = 1,
    prefer_full_nodes: bool = True,
) -> Optional[GPUAvailability]:
    """Find the best compute group using accurate browser API data."""
    if prefer_full_nodes:
        try:
            from inspire.cli.utils.resources import fetch_resource_availability

            node_availability = fetch_resource_availability(known_only=not preferred_groups)
            gpu_type_upper = (gpu_type or "").upper()
            required_instances = max(1, int(instance_count))
            normalized_min_gpus = max(1, int(min_gpus))

            candidates = []
            for group in node_availability:
                if gpu_type_upper and gpu_type_upper != "ANY":
                    if gpu_type_upper not in (group.gpu_type or "").upper():
                        continue

                gpu_per_node = group.gpu_per_node or 0
                if gpu_per_node <= 0:
                    continue

                nodes_per_instance = math.ceil(normalized_min_gpus / gpu_per_node)
                required_nodes = required_instances * nodes_per_instance
                if group.free_nodes < required_nodes:
                    continue

                candidates.append(group)

            if candidates:
                candidates.sort(
                    key=lambda g: (g.free_nodes, g.free_gpus),
                    reverse=True,
                )

                selected = None
                if preferred_groups:
                    for group in candidates:
                        if group.group_id in preferred_groups:
                            selected = group
                            break

                if selected is None:
                    selected = candidates[0]

                total_gpus = selected.total_nodes * selected.gpu_per_node
                used_gpus = max(total_gpus - selected.free_gpus, 0)

                return GPUAvailability(
                    group_id=selected.group_id,
                    group_name=selected.group_name,
                    gpu_type=selected.gpu_type,
                    total_gpus=total_gpus,
                    used_gpus=used_gpus,
                    available_gpus=selected.free_gpus,
                    low_priority_gpus=0,
                    free_nodes=selected.free_nodes,
                    gpu_per_node=selected.gpu_per_node,
                    selection_source="nodes",
                )
        except Exception:
            pass

    availability = get_accurate_gpu_availability()
    if not availability:
        return None

    def effective_available(group: GPUAvailability) -> int:
        if include_preemptible:
            return group.available_gpus + group.low_priority_gpus
        return group.available_gpus

    if gpu_type and gpu_type.upper() != "ANY":
        gpu_type_upper = gpu_type.upper()
        filtered = [g for g in availability if gpu_type_upper in g.gpu_type.upper()]
    else:
        filtered = list(availability)

    filtered.sort(key=effective_available, reverse=True)

    if preferred_groups:
        for group in filtered:
            if group.group_id in preferred_groups and effective_available(group) >= min_gpus:
                return group

    for group in filtered:
        if effective_available(group) >= min_gpus:
            return group

    return None


@dataclass
class FullFreeNodeCount:
    """Full-free (idle) node counts for a compute group."""

    group_id: str
    group_name: str
    gpu_per_node: int
    total_nodes: int
    ready_nodes: int
    full_free_nodes: int


def get_full_free_node_counts(
    group_ids: list[str],
    *,
    gpu_per_node: int = 8,
    session: Optional[WebSession] = None,
    _retry: bool = True,
) -> list[FullFreeNodeCount]:
    """Get per-group counts of fully-free nodes using the browser API."""
    if session is None:
        session = get_web_session()

    results: list[FullFreeNodeCount] = []

    try:
        for gid in group_ids:
            body = {
                "page_num": 1,
                "page_size": -1,
                "filter": {"logic_compute_group_id": gid},
            }

            payload = _request_json(
                session,
                "POST",
                _browser_api_path("/cluster_nodes/list"),
                referer=f"{BASE_URL}/jobs/distributedTraining",
                body=body,
                timeout=30,
            )

            if payload.get("code") != 0:
                raise ValueError(f"API error: {payload.get('message')}")

            data = payload.get("data", {})
            nodes = data.get("nodes", []) or []

            total_nodes = len(nodes)
            ready_nodes = 0
            full_free_nodes = 0
            group_name = ""

            for node in nodes:
                if not group_name:
                    group_name = node.get("logic_compute_group_name", "") or ""

                status = (node.get("status") or "").upper()
                if status == "READY":
                    ready_nodes += 1

                    node_gpu = node.get("gpu_count", 0) or 0
                    task_list = node.get("task_list") or []
                    if node_gpu == gpu_per_node and len(task_list) == 0:
                        full_free_nodes += 1

            results.append(
                FullFreeNodeCount(
                    group_id=gid,
                    group_name=group_name,
                    gpu_per_node=gpu_per_node,
                    total_nodes=total_nodes,
                    ready_nodes=ready_nodes,
                    full_free_nodes=full_free_nodes,
                )
            )

    except SessionExpiredError:
        if _retry:
            clear_session_cache()
            return get_full_free_node_counts(
                group_ids,
                gpu_per_node=gpu_per_node,
                session=None,
                _retry=False,
            )
        raise

    results.sort(key=lambda r: r.full_free_nodes, reverse=True)
    return results
