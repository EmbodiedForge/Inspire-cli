"""Resource availability utilities for smart GPU allocation.

Uses the /openapi/v1/cluster_nodes/list API to get real-time GPU availability
across compute groups.
"""

from __future__ import annotations

import time
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional
from enum import Enum

from inspire.cli.utils.auth import AuthManager
from inspire.cli.utils.config import Config


class GPUType(Enum):
    """GPU types available in the cluster."""
    H100 = "H100"
    H200 = "H200"


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
    online_nodes: int = 0  # resource_pool == "online"
    backup_nodes: int = 0  # resource_pool == "backup"
    fault_nodes: int = 0   # resource_pool == "fault"


# Known compute groups for smart allocation
# Only these groups will be used for auto-selection
KNOWN_COMPUTE_GROUPS = {
    # H100 groups
    "lcg-79b2ad0e-a375-43f3-a0b1-b4ce79710fd7": "H100 CUDA 12.8",
    # H200 groups
    "lcg-df089db8-817a-4aa8-a164-eb1a32948564": "H200 机房1",
    "lcg-303ac8c6-aa19-4284-af03-2296592326e5": "H200 机房2",
    "lcg-a91ad10b-415d-4abd-8170-828a2feae5d2": "H200 机房3",
    "lcg-95e38be4-4842-4155-af13-4325aa744bca": "H200 3号-2",
}


# Cache for availability data
_availability_cache: Optional[dict] = None
_cache_time: float = 0
_CACHE_TTL = 30  # seconds


def _normalize_gpu_type(display_name: str) -> str:
    """Normalize GPU type display name to short form (H100/H200/PPU ZW810/etc)."""
    display_upper = display_name.upper()
    if "H100" in display_upper:
        return GPUType.H100.value
    elif "H200" in display_upper:
        return GPUType.H200.value
    elif "PPU" in display_upper or "ZW810" in display_upper:
        return "PPU ZW810"
    # For other types, extract the main identifier (before parentheses)
    if "(" in display_name:
        return display_name.split("(")[0].strip()
    return display_name


def fetch_resource_availability(
    config: Config,
    known_only: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[ComputeGroupAvailability]:
    """Fetch real-time GPU availability from all compute groups.

    Iterates through paginated results from /openapi/v1/cluster_nodes/list,
    groups nodes by logic_compute_group_id, and calculates availability.

    Args:
        config: CLI configuration
        known_only: If True, only return known compute groups (for auto-selection)
        progress_callback: Optional callback(fetched, total) for progress updates

    Returns:
        List of ComputeGroupAvailability sorted by free_gpus (descending)
    """
    global _availability_cache, _cache_time

    # Check cache
    if _availability_cache and (time.time() - _cache_time < _CACHE_TTL):
        cache_key = "known" if known_only else "all"
        if cache_key in _availability_cache:
            return _availability_cache[cache_key]

    api = AuthManager.get_api(config)

    # Fetch nodes from API
    # Note: API requires 0.6s between requests and has rate limiting.
    # Use larger page size (API allows up to 1000) to reduce request count.

    groups: dict[str, dict] = {}

    page_num = 1
    page_size = 1000
    fetched = 0

    while True:
        result = api.list_cluster_nodes(
            page_num=page_num,
            page_size=page_size,
            resource_pool=None,
        )

        nodes = result.get("data", {}).get("nodes", [])
        if not nodes:
            break

        for node in nodes:
            # Only include GPU nodes
            if node.get("gpu_count", 0) == 0:
                continue

            group_id = node.get("logic_compute_group_id", "")
            if not group_id:
                continue

            # Skip if known_only and group is not known
            if known_only and group_id not in KNOWN_COMPUTE_GROUPS:
                continue

            if group_id not in groups:
                gpu_info = node.get("gpu_info", {})
                gpu_display = gpu_info.get("gpu_type_display", "Unknown")
                gpu_type = _normalize_gpu_type(gpu_display)

                groups[group_id] = {
                    "group_id": group_id,
                    "group_name": node.get("logic_compute_group_name", "Unknown"),
                    "gpu_type": gpu_type,
                    "gpu_per_node": node.get("gpu_count", 0),
                    "total_nodes": 0,
                    "ready_nodes": 0,
                    "free_nodes": 0,
                    "online_nodes": 0,
                    "backup_nodes": 0,
                    "fault_nodes": 0,
                }

            groups[group_id]["total_nodes"] += 1

            # Count by resource_pool status
            resource_pool = node.get("resource_pool", "unknown")
            if resource_pool == "online":
                groups[group_id]["online_nodes"] += 1
            elif resource_pool == "backup":
                groups[group_id]["backup_nodes"] += 1
            elif resource_pool == "fault":
                groups[group_id]["fault_nodes"] += 1

            if node.get("status") == "READY":
                groups[group_id]["ready_nodes"] += 1

                task_list = node.get("task_list", [])
                if not task_list or len(task_list) == 0:
                    groups[group_id]["free_nodes"] += 1

        fetched += len(nodes)
        total = result.get("data", {}).get("total", 0)

        # Report progress
        if progress_callback and total:
            progress_callback(fetched, total)

        if total and fetched >= total:
            break

        page_num += 1
        time.sleep(0.7)

    # Convert to ComputeGroupAvailability objects
    availability_list = []
    for group_data in groups.values():
        free_gpus = group_data["free_nodes"] * group_data["gpu_per_node"]
        availability_list.append(
            ComputeGroupAvailability(
                group_id=group_data["group_id"],
                group_name=group_data["group_name"],
                gpu_type=group_data["gpu_type"],
                gpu_per_node=group_data["gpu_per_node"],
                total_nodes=group_data["total_nodes"],
                ready_nodes=group_data["ready_nodes"],
                free_nodes=group_data["free_nodes"],
                free_gpus=free_gpus,
                online_nodes=group_data["online_nodes"],
                backup_nodes=group_data["backup_nodes"],
                fault_nodes=group_data["fault_nodes"],
            )
        )

    # Sort by free_gpus descending
    availability_list.sort(key=lambda x: x.free_gpus, reverse=True)

    # Update cache
    if _availability_cache is None:
        _availability_cache = {}
    _availability_cache["known" if known_only else "all"] = availability_list
    _cache_time = time.time()

    return availability_list


def find_best_compute_group(
    availability: list[ComputeGroupAvailability],
    gpu_type: Optional[str] = None,
    min_gpus: int = 8,
    preferred_groups: Optional[list[str]] = None,
) -> Optional[ComputeGroupAvailability]:
    """Find the compute group with most available capacity.

    Args:
        availability: List of compute group availability
        gpu_type: Filter by GPU type ("H100", "H200", or None for any)
        min_gpus: Minimum required GPUs
        preferred_groups: Preferred group IDs (checked first)

    Returns:
        Best matching ComputeGroupAvailability, or None if no suitable group found
    """
    # Filter by GPU type
    if gpu_type and gpu_type.upper() != "ANY":
        filtered = [g for g in availability if g.gpu_type.upper() == gpu_type.upper()]
    else:
        filtered = availability

    # Check preferred groups first
    if preferred_groups:
        for group in filtered:
            if group.group_id in preferred_groups and group.free_gpus >= min_gpus:
                return group

    # Find group with most available GPUs that meets min_gpus
    for group in filtered:
        if group.free_gpus >= min_gpus:
            return group

    return None


def clear_availability_cache() -> None:
    """Clear the availability cache."""
    global _availability_cache, _cache_time
    _availability_cache = None
    _cache_time = 0
