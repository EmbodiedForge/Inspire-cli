"""Models for browser (web-session) availability APIs."""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass
class FullFreeNodeCount:
    """Full-free (idle) node counts for a compute group."""

    group_id: str
    group_name: str
    gpu_per_node: int
    total_nodes: int
    ready_nodes: int
    full_free_nodes: int


__all__ = ["FullFreeNodeCount", "GPUAvailability"]
