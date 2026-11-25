"""Resource availability utilities for smart GPU allocation.

Uses browser API endpoints to get accurate GPU availability across
compute groups (matching the web UI).
"""

from __future__ import annotations

import os
import time
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Callable, Optional, TypedDict, cast
from enum import Enum

from inspire.config import Config
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import (
    SessionExpiredError,
    WebSession,
    fetch_node_dimensions,
    get_web_session,
)
from inspire.compute_groups import compute_group_name_map, load_compute_groups_from_config

logger = logging.getLogger(__name__)


class _AvailabilityRequest(TypedDict, total=False):
    workspace_id: str
    session: WebSession
    gpu_type: str
    compute_groups: list[dict]


class GPUType(Enum):
    """GPU types available in the cluster."""

    H100 = "H100"
    H200 = "H200"
    RTX4090 = "4090"


@dataclass
class ComputeGroupAvailability:
    """GPU availability for a compute group."""

    group_id: str
    group_name: str
    gpu_type: str
    gpu_per_node: int
    total_nodes: int
    ready_nodes: int
    free_nodes: int
    free_gpus: int  # free_nodes * gpu_per_node
    gpu_full_free_nodes: int = 0
    schedulable_full_nodes: int = 0
    partial_free_nodes: int = 0
    gpu_available_from_nodes: int = 0
    low_priority_nodes_estimate: int = 0
    request_fit_nodes: int = 0
    request_fit_gpus: int = 0
    availability_source: str = "nodes"
    fallback_reason: str = ""
    online_nodes: int = 0  # resource_pool == "online"
    backup_nodes: int = 0  # resource_pool == "backup"
    fault_nodes: int = 0  # resource_pool == "fault"
    workspace_ids: list[str] = field(default_factory=list)
    workspace_aliases: list[str] = field(default_factory=list)
    low_priority_gpus: int | None = None
    low_priority_source: str | None = None


# Known compute groups for smart allocation
# Only these groups will be used for auto-selection
# Will be initialized on first use from config
KNOWN_COMPUTE_GROUPS: dict[str, str] = {}


# Cache for availability data
_availability_cache: Optional[dict] = None
_cache_time: float = 0
_CACHE_TTL = 30  # seconds

_UNSCHEDULABLE_RESOURCE_POOLS = {"backup", "fault"}


def is_schedulable_resource_pool(value: object) -> bool:
    """Return whether a node pool accepts ordinary workload pods."""
    return str(value or "unknown").strip().lower() not in _UNSCHEDULABLE_RESOURCE_POOLS


def _normalize_gpu_type(display_name: str) -> str:
    """Normalize GPU type display name to short form (H100/H200/PPU ZW810/etc)."""
    display_upper = display_name.upper()
    if "H100" in display_upper:
        return GPUType.H100.value
    elif "H200" in display_upper:
        return GPUType.H200.value
    elif "4090" in display_upper:
        return GPUType.RTX4090.value
    elif "PPU" in display_upper or "ZW810" in display_upper:
        return "PPU ZW810"
    # For other types, extract the main identifier (before parentheses)
    if "(" in display_name:
        return display_name.split("(")[0].strip()
    return display_name


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(cast(Any, value or 0))
    except (TypeError, ValueError):
        return default


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(cast(Any, value or 0))
    except (TypeError, ValueError):
        return default


def node_is_schedulable(node: dict) -> bool:
    """Whether the scheduler may place an ordinary workload on this node.

    READY alone is not enough: a cordoned, maintenance, backup, or fault node is
    still reported by the console's node view, but it must not contribute to a
    capacity number that callers compare with scheduler admission.
    """
    status = str(node.get("status") or "").strip().upper()
    if status != "READY":
        return False
    if str(node.get("cordon_type") or "").strip():
        return False
    if bool(node.get("is_maint", False)):
        return False
    return is_schedulable_resource_pool(node.get("resource_pool"))


def _task_count(node: dict) -> int:
    tasks_associated = node.get("tasks_associated")
    if isinstance(tasks_associated, dict):
        return _safe_int(tasks_associated.get("count"), 0)
    if isinstance(tasks_associated, list):
        return len(tasks_associated)
    task_list = node.get("task_list")
    if isinstance(task_list, list):
        return len(task_list)
    return 0


def _summarize_node_dimensions(
    nodes: list[dict],
    *,
    group_id: str,
    group_name: str,
    gpu_type: str,
    workspace_id: str,
    workspace_aliases: list[str],
    low_priority_gpus: int | None,
    low_priority_source: str | None = None,
    required_gpu_count: int = 0,
    required_cpu_count: int = 0,
    required_memory_gib: int = 0,
) -> ComputeGroupAvailability:
    total_nodes = 0
    ready_nodes = 0
    gpu_full_free_nodes = 0
    gpu_full_free_gpus = 0
    schedulable_full_nodes = 0
    schedulable_full_gpus = 0
    partial_free_nodes = 0
    gpu_available_from_nodes = 0
    request_fit_nodes = 0
    request_fit_gpus = 0
    gpu_total_from_nodes = 0
    gpu_per_node = 0
    online_nodes = 0
    backup_nodes = 0
    fault_nodes = 0

    # Node CPU/memory totals vary by generation, including within a compute
    # group.  Compare every node with its own shape instead of using the
    # smallest node as a group-wide threshold.
    known_shape_nodes = 0

    for node in nodes:
        gpu_info = node.get("gpu") or {}
        node_gpu_total = _safe_int(gpu_info.get("total"), 0)
        if node_gpu_total <= 0:
            continue

        total_nodes += 1
        gpu_total_from_nodes += node_gpu_total
        gpu_per_node = max(gpu_per_node, node_gpu_total)
        gpu_available = _safe_int(gpu_info.get("available"), 0)

        resource_pool = str(node.get("resource_pool") or "unknown").lower()
        if resource_pool == "online":
            online_nodes += 1
        elif resource_pool == "backup":
            backup_nodes += 1
        elif resource_pool == "fault":
            fault_nodes += 1

        status = str(node.get("status") or "").upper()
        if status == "READY":
            ready_nodes += 1

        cpu_available = _safe_float((node.get("cpu") or {}).get("available"))
        memory_available = _safe_float((node.get("memory") or {}).get("available"))
        cpu_total = _safe_float((node.get("cpu") or {}).get("total"))
        memory_total = _safe_float((node.get("memory") or {}).get("total"))
        has_known_node_shape = cpu_total > 0 and memory_total > 0
        if has_known_node_shape:
            known_shape_nodes += 1

        cordon_type = str(node.get("cordon_type") or "").strip()
        is_maint = bool(node.get("is_maint", False))
        is_schedulable_node = (
            status == "READY"
            and not cordon_type
            and not is_maint
            and is_schedulable_resource_pool(resource_pool)
        )
        if is_schedulable_node:
            gpu_available_from_nodes += max(gpu_available, 0)
        if (
            required_gpu_count > 0
            and is_schedulable_node
            and gpu_available >= required_gpu_count
            and cpu_available >= required_cpu_count
            and memory_available >= required_memory_gib
        ):
            request_fit_nodes += 1
            request_fit_gpus += required_gpu_count
        is_gpu_full_free = (
            is_schedulable_node and gpu_available >= node_gpu_total and _task_count(node) == 0
        )
        if is_gpu_full_free:
            gpu_full_free_nodes += 1
            gpu_full_free_gpus += node_gpu_total
            if (
                has_known_node_shape
                and cpu_available >= cpu_total
                and memory_available >= memory_total
            ):
                schedulable_full_nodes += 1
                schedulable_full_gpus += node_gpu_total
        elif is_schedulable_node and 0 < gpu_available < node_gpu_total:
            partial_free_nodes += 1

    has_known_full_node_shape = known_shape_nodes > 0
    if not gpu_per_node and total_nodes:
        gpu_per_node = max(1, gpu_total_from_nodes // total_nodes)
    selection_free_nodes = (
        schedulable_full_nodes if has_known_full_node_shape else gpu_full_free_nodes
    )
    availability_source = "nodes" if has_known_full_node_shape else "nodes-gpu"
    free_gpus = schedulable_full_gpus if has_known_full_node_shape else gpu_full_free_gpus
    low_priority_nodes_estimate = (low_priority_gpus or 0) // max(1, gpu_per_node or 8)

    return ComputeGroupAvailability(
        group_id=group_id,
        group_name=group_name,
        gpu_type=gpu_type,
        gpu_per_node=gpu_per_node,
        total_nodes=total_nodes,
        ready_nodes=ready_nodes,
        free_nodes=selection_free_nodes,
        free_gpus=free_gpus,
        gpu_full_free_nodes=gpu_full_free_nodes,
        schedulable_full_nodes=schedulable_full_nodes,
        partial_free_nodes=partial_free_nodes,
        gpu_available_from_nodes=gpu_available_from_nodes,
        low_priority_nodes_estimate=low_priority_nodes_estimate,
        request_fit_nodes=request_fit_nodes,
        request_fit_gpus=request_fit_gpus,
        availability_source=availability_source,
        online_nodes=online_nodes,
        backup_nodes=backup_nodes,
        fault_nodes=fault_nodes,
        workspace_ids=[workspace_id],
        workspace_aliases=list(workspace_aliases),
        low_priority_gpus=low_priority_gpus,
        low_priority_source=low_priority_source,
    )


def _availability_from_node_summary(
    *,
    group: object,
    workspace_id: str,
    workspace_aliases: list[str],
    fallback_reason: str,
) -> ComputeGroupAvailability:
    group_id = str(getattr(group, "group_id", "") or "").strip()
    group_name = str(getattr(group, "group_name", "") or group_id)
    gpu_type = _normalize_gpu_type(str(getattr(group, "gpu_type", "") or "Unknown"))
    total_gpus = _safe_int(getattr(group, "total_gpus", 0), 0)
    available_gpus = max(_safe_int(getattr(group, "available_gpus", 0), 0), 0)
    low_priority_gpus = getattr(group, "low_priority_gpus", None)
    gpu_per_node = _safe_int(getattr(group, "gpu_per_node", 0), 0) or 8
    free_nodes = available_gpus // gpu_per_node

    return ComputeGroupAvailability(
        group_id=group_id,
        group_name=group_name,
        gpu_type=gpu_type,
        gpu_per_node=gpu_per_node,
        total_nodes=total_gpus // gpu_per_node if gpu_per_node else 0,
        ready_nodes=0,
        free_nodes=free_nodes,
        free_gpus=free_nodes * gpu_per_node,
        gpu_full_free_nodes=free_nodes,
        schedulable_full_nodes=0,
        partial_free_nodes=0,
        gpu_available_from_nodes=available_gpus,
        low_priority_nodes_estimate=(low_priority_gpus or 0) // max(1, gpu_per_node),
        availability_source="nodes-gpu",
        fallback_reason=fallback_reason,
        workspace_ids=[workspace_id],
        workspace_aliases=list(workspace_aliases),
        low_priority_gpus=low_priority_gpus,
        low_priority_source=getattr(group, "low_priority_source", None),
    )


def _unavailable_availability(
    *,
    group: object,
    workspace_id: str,
    workspace_aliases: list[str],
    fallback_reason: str,
) -> ComputeGroupAvailability:
    group_id = str(getattr(group, "group_id", "") or "").strip()
    group_name = str(getattr(group, "group_name", "") or group_id)
    gpu_type = _normalize_gpu_type(str(getattr(group, "gpu_type", "") or "Unknown"))
    return ComputeGroupAvailability(
        group_id=group_id,
        group_name=group_name,
        gpu_type=gpu_type,
        gpu_per_node=0,
        total_nodes=0,
        ready_nodes=0,
        free_nodes=0,
        free_gpus=0,
        gpu_full_free_nodes=0,
        schedulable_full_nodes=0,
        partial_free_nodes=0,
        gpu_available_from_nodes=0,
        low_priority_nodes_estimate=0,
        availability_source="unavailable",
        fallback_reason=fallback_reason,
        workspace_ids=[workspace_id],
        workspace_aliases=list(workspace_aliases),
        low_priority_gpus=None,
        low_priority_source=None,
    )


def _observation_rank(item: ComputeGroupAvailability) -> tuple[bool, bool, int, int]:
    return (
        item.availability_source != "aggregate",
        item.availability_source == "nodes",
        item.total_nodes,
        item.ready_nodes,
    )


def _merge_availability_by_logical_group(
    entries: list[ComputeGroupAvailability],
) -> list[ComputeGroupAvailability]:
    observations: dict[str, list[ComputeGroupAvailability]] = {}
    for entry in entries:
        observations.setdefault(entry.group_id, []).append(entry)

    merged: list[ComputeGroupAvailability] = []
    for group_observations in observations.values():
        preferred = max(group_observations, key=_observation_rank)
        if len(group_observations) == 1:
            merged.append(preferred)
            continue

        preferred = replace(preferred)
        preferred.workspace_ids = list(
            dict.fromkeys(
                workspace_id for item in group_observations for workspace_id in item.workspace_ids
            )
        )
        preferred.workspace_aliases = list(
            dict.fromkeys(alias for item in group_observations for alias in item.workspace_aliases)
        )
        fallback_evidence = []
        for item in group_observations:
            if not item.fallback_reason:
                continue
            workspaces = ", ".join(item.workspace_ids) or "unknown workspace"
            fallback_evidence.append(f"{workspaces}: {item.fallback_reason}")
        preferred.fallback_reason = "; ".join(dict.fromkeys(fallback_evidence))
        merged.append(preferred)

    return merged


def fetch_resource_availability(
    config: Optional[Config] = None,
    known_only: bool = False,
    workspace_ids: Optional[list[str]] = None,
    workspace_aliases_by_id: Optional[dict[str, list[str]]] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    workspace_error_callback: Optional[Callable[[str, Exception], None]] = None,
    gpu_type_filter: Optional[str] = None,
    node_summary_by_workspace: Optional[dict[str, list[object]]] = None,
    candidate_groups: Optional[list[dict]] = None,
    request_specs_by_group: Optional[dict[str, dict[str, int]]] = None,
    session: Optional[WebSession] = None,
) -> list[ComputeGroupAvailability]:
    """Fetch real-time GPU availability from compute groups.

    Uses ``workspace?Action=ListNodeDimension`` to compute per-node availability.
    The node view is available to normal workspace users; the older
    ``cluster?Action=ListNodeDimension`` service answers AccessForbidden even though the
    console can show the same data.

    Args:
        config: Optional CLI configuration (used for base_url override and compute_groups)
        known_only: If True, only return known compute groups (for auto-selection)
        progress_callback: Optional callback(fetched, total) for progress updates
        gpu_type_filter: Optional GPU label used to avoid probing explicitly
            conflicting compute groups.
        node_summary_by_workspace: Optional output mapping populated with the
            group-level node totals fetched before the per-node detail probe.
        candidate_groups: Optional launchable group subset. When supplied, the
            aggregate fetch reuses it instead of listing every workspace group.
        request_specs_by_group: Per-group GPU/CPU/memory requirements used to
            count nodes that can fit one requested notebook instance.
        session: Optional authenticated session shared with the planning phase.

    Returns:
        List of ComputeGroupAvailability sorted by free_gpus (descending)
    """
    global _availability_cache, _cache_time, KNOWN_COMPUTE_GROUPS

    resolved_config = config

    if resolved_config is None:
        try:
            resolved_config, _ = Config.from_files_and_env(
                require_credentials=False,
                require_target_dir=False,
            )
        except Exception:
            resolved_config = None

    def resolve_session() -> WebSession:
        if session is not None:
            return session
        account_alias = getattr(resolved_config, "account_alias", None)
        if account_alias:
            return get_web_session(require_workspace=True, account=account_alias)
        return get_web_session(require_workspace=True)

    # Load known compute groups from config
    known_groups_map: dict[str, str] = {}
    if resolved_config is not None and hasattr(resolved_config, "compute_groups"):
        compute_groups_tuples = load_compute_groups_from_config(resolved_config.compute_groups)
        known_groups_map = compute_group_name_map(compute_groups_tuples)
    # Update global for backward compatibility
    KNOWN_COMPUTE_GROUPS = known_groups_map

    # Check cache
    requested_workspace_ids = [
        str(ws or "").strip() for ws in (workspace_ids or []) if str(ws or "").strip()
    ]
    resolved_session = session
    if not requested_workspace_ids:
        resolved_session = resolve_session()
        requested_workspace_ids = [str(resolved_session.workspace_id or "").strip()]
    requested_workspace_ids = sorted(set(requested_workspace_ids))

    normalized_gpu_filter = str(gpu_type_filter or "all").strip().upper()
    candidate_ids = sorted(
        str(group.get("logic_compute_group_id") or "").strip()
        for group in (candidate_groups or [])
        if str(group.get("logic_compute_group_id") or "").strip()
    )
    normalized_request_specs = request_specs_by_group or {}
    request_key = ",".join(
        f"{group_id}:{spec.get('gpu_count', 0)}:{spec.get('cpu_count', 0)}:"
        f"{spec.get('memory_size_gib', 0)}"
        for group_id, spec in sorted(normalized_request_specs.items())
    )
    cache_key = (
        f"{'known' if known_only else 'all'}:{normalized_gpu_filter}:"
        f"{','.join(requested_workspace_ids)}:{','.join(candidate_ids)}:{request_key}"
    )
    if _availability_cache and (time.time() - _cache_time < _CACHE_TTL):
        if cache_key in _availability_cache:
            return _availability_cache[cache_key]

    base_url = None
    if resolved_config is not None:
        base_url = getattr(resolved_config, "base_url", None)
    if not base_url:
        base_url = os.environ.get("INSPIRE_BASE_URL", "https://api.example.com").strip()

    availability_list: list[ComputeGroupAvailability] = []
    if resolved_session is None:
        resolved_session = resolve_session()
    session = resolved_session
    workspace_aliases_by_id = workspace_aliases_by_id or {}
    total_workspaces = len(requested_workspace_ids)
    had_workspace_errors = False

    for ws_index, workspace_id in enumerate(requested_workspace_ids, start=1):
        workspace_aliases = workspace_aliases_by_id.get(workspace_id, [])
        try:
            summary_availability: Sequence[object]
            if candidate_groups is not None and normalized_request_specs:
                # Multi-GPU notebook selection already has authoritative GPU
                # type and launchability from notebook offers. Query nodes
                # directly instead of spending one aggregate request per group
                # before the request-fit probe.
                summary_availability = [
                    SimpleNamespace(
                        group_id=str(group.get("logic_compute_group_id") or "").strip(),
                        group_name=str(group.get("name") or "").strip(),
                        gpu_type=str(group.get("gpu_type") or "Unknown"),
                        total_gpus=1,
                        low_priority_gpus=0,
                    )
                    for group in candidate_groups
                    if str(group.get("logic_compute_group_id") or "").strip()
                ]
            else:
                summary_kwargs: _AvailabilityRequest = {
                    "workspace_id": workspace_id,
                    "session": session,
                }
                if gpu_type_filter:
                    summary_kwargs["gpu_type"] = gpu_type_filter
                if candidate_groups is not None:
                    summary_kwargs["compute_groups"] = candidate_groups
                summary_availability = browser_api_module.get_accurate_gpu_availability(
                    **summary_kwargs
                )
                if node_summary_by_workspace is not None:
                    node_summary_by_workspace[workspace_id] = list(summary_availability)
        except SessionExpiredError:
            raise
        except Exception as exc:
            if workspace_error_callback is None:
                raise
            had_workspace_errors = True
            workspace_error_callback(workspace_id, exc)
            if progress_callback and total_workspaces:
                progress_callback(ws_index, total_workspaces)
            continue
        if progress_callback and total_workspaces:
            progress_callback(ws_index, total_workspaces)

        for summary in summary_availability:
            if (
                getattr(summary, "total_gpus", 0) <= 0
                and getattr(summary, "selection_source", "") != "unavailable"
            ):
                continue
            group_id = str(getattr(summary, "group_id", "") or "").strip()
            if known_only and group_id not in known_groups_map:
                continue
            if not group_id:
                continue

            group_name = str(getattr(summary, "group_name", "") or "").strip()
            if not group_name and group_id in known_groups_map:
                group_name = known_groups_map[group_id]
            if not group_name:
                group_name = "Unknown"
            gpu_type = _normalize_gpu_type(str(getattr(summary, "gpu_type", "") or "Unknown"))
            request_spec = normalized_request_specs.get(group_id, {})

            if getattr(summary, "selection_source", "") == "unavailable":
                availability_list.append(
                    _unavailable_availability(
                        group=summary,
                        workspace_id=workspace_id,
                        workspace_aliases=workspace_aliases,
                        fallback_reason=str(
                            getattr(summary, "fallback_reason", "")
                            or "node-level visibility unavailable"
                        ),
                    )
                )
                continue

            try:
                data = fetch_node_dimensions(
                    session,
                    base_url=base_url,
                    workspace_id=workspace_id,
                    logic_compute_group_id=group_id,
                )
                nodes = data.get("data", {}).get("node_dimensions", []) or []
                availability_list.append(
                    _summarize_node_dimensions(
                        nodes,
                        group_id=group_id,
                        group_name=group_name,
                        gpu_type=gpu_type,
                        workspace_id=workspace_id,
                        workspace_aliases=workspace_aliases,
                        low_priority_gpus=getattr(summary, "low_priority_gpus", None),
                        low_priority_source=getattr(summary, "low_priority_source", None),
                        required_gpu_count=_safe_int(request_spec.get("gpu_count"), 0),
                        required_cpu_count=_safe_int(request_spec.get("cpu_count"), 0),
                        required_memory_gib=_safe_int(request_spec.get("memory_size_gib"), 0),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Node-dimension availability failed for %s: %s", group_id, exc)
                availability_list.append(
                    _availability_from_node_summary(
                        group=summary,
                        workspace_id=workspace_id,
                        workspace_aliases=workspace_aliases,
                        fallback_reason=str(exc),
                    )
                )

    availability_list = _merge_availability_by_logical_group(availability_list)
    availability_list.sort(
        key=lambda x: (
            x.free_nodes,
            x.gpu_full_free_nodes,
            x.low_priority_nodes_estimate,
            x.gpu_available_from_nodes,
        ),
        reverse=True,
    )

    # Update cache
    if not had_workspace_errors:
        if _availability_cache is None:
            _availability_cache = {}
        _availability_cache[cache_key] = availability_list
        _cache_time = time.time()

    return availability_list


def clear_availability_cache() -> None:
    """Clear the availability cache."""
    global _availability_cache, _cache_time
    _availability_cache = None
    _cache_time = 0
