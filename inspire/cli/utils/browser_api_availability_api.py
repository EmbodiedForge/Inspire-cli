"""Browser (web-session) APIs for compute group availability (HTTP endpoints)."""

from __future__ import annotations

from typing import Optional

from inspire.cli.utils.browser_api_availability_models import FullFreeNodeCount, GPUAvailability
from inspire.cli.utils.browser_api_core import BASE_URL, _browser_api_path, _request_json
from inspire.cli.utils.web_session import (
    DEFAULT_WORKSPACE_ID,
    SessionExpiredError,
    WebSession,
    clear_session_cache,
    get_web_session,
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


__all__ = [
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "list_compute_groups",
]
