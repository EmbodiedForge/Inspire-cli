"""Browser (web-session) APIs for compute group availability and selection.

This module currently re-exports availability functions from the legacy implementation.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_legacy import (  # noqa: F401
    FullFreeNodeCount,
    GPUAvailability,
    find_best_compute_group_accurate,
    get_accurate_gpu_availability,
    get_full_free_node_counts,
    list_compute_groups,
)

__all__ = [
    "FullFreeNodeCount",
    "GPUAvailability",
    "find_best_compute_group_accurate",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "list_compute_groups",
]

