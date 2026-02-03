"""Browser (web-session) APIs for compute group availability and selection.

The web UI exposes endpoints for:
- aggregated GPU usage per compute group
- per-node (fragmentation-aware) "full free node" availability

These endpoints require a web-session cookie and are not part of the OpenAPI surface.

This module re-exports the public API from smaller `browser_api_availability_*` modules
to keep historical import paths stable.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_availability_api import (  # noqa: F401
    get_accurate_gpu_availability,
    get_full_free_node_counts,
    list_compute_groups,
)
from inspire.cli.utils.browser_api_availability_models import (  # noqa: F401
    FullFreeNodeCount,
    GPUAvailability,
)
from inspire.cli.utils.browser_api_availability_select import (  # noqa: F401
    find_best_compute_group_accurate,
)

__all__ = [
    "FullFreeNodeCount",
    "GPUAvailability",
    "find_best_compute_group_accurate",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "list_compute_groups",
]
