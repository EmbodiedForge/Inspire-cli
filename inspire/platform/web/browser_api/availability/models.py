"""Models for browser (web-session) availability APIs."""

from __future__ import annotations

from dataclasses import dataclass, field

PLACEMENT_AGGREGATE = "aggregate-gpu-count"
PLACEMENT_FULL_FREE_NODES = "full-free-nodes"
PLACEMENT_REQUEST_FIT_NODES = "request-fit-nodes"

# How much each reported capacity figure is actually evidence of.  A group-wide GPU
# total never shows whether the GPUs sit on one node or on sixteen fragments, so it
# is capacity, not schedulability; the node-level bases say which node check ran.
PLACEMENT_BASIS_BY_SOURCE: dict[str, str] = {
    "nodes-fit": PLACEMENT_REQUEST_FIT_NODES,
    "nodes": PLACEMENT_FULL_FREE_NODES,
    "aggregate": PLACEMENT_AGGREGATE,
}

PLACEMENT_CAVEATS: dict[str, str] = {
    PLACEMENT_AGGREGATE: (
        "group-wide aggregate GPU count: capacity, not schedulability - node "
        "fragmentation and CPU/memory fit are unproven, so the launch may queue"
    ),
    PLACEMENT_FULL_FREE_NODES: (
        "node-level full-free node count: whole nodes were counted, so the launch "
        "shape fits; the scheduler still has the last word"
    ),
    PLACEMENT_REQUEST_FIT_NODES: (
        "node-level request-fit count: GPUs, CPU and memory were checked per node "
        "against this request; the scheduler still has the last word"
    ),
}


def placement_basis_for_source(selection_source: str | None) -> str:
    """Classify what a reported capacity figure is evidence of."""
    source = str(selection_source or "").strip()
    basis = PLACEMENT_BASIS_BY_SOURCE.get(source)
    if basis is None:
        basis = PLACEMENT_FULL_FREE_NODES if source.startswith("nodes") else PLACEMENT_AGGREGATE
    return basis


@dataclass
class GPUAvailability:
    """GPU availability for a compute group."""

    group_id: str
    group_name: str
    gpu_type: str
    total_gpus: int
    used_gpus: int
    available_gpus: int
    low_priority_gpus: int | None = None  # Advisory aggregate observation; not free capacity.
    low_priority_source: str | None = None
    free_nodes: int = 0
    gpu_per_node: int = 0
    gpu_full_free_nodes: int = 0
    schedulable_full_nodes: int = 0
    partial_free_nodes: int = 0
    low_priority_nodes_estimate: int = 0
    selection_source: str = "aggregate"
    fallback_reason: str = ""
    workspace_ids: list[str] = field(default_factory=list)
    workspace_aliases: list[str] = field(default_factory=list)

    @property
    def placement_basis(self) -> str:
        """Which node-level check, if any, the reported capacity rests on."""
        return placement_basis_for_source(self.selection_source)

    @property
    def placement_caveat(self) -> str:
        """What the reported capacity does and does not prove about this launch."""
        return PLACEMENT_CAVEATS[self.placement_basis]


__all__ = [
    "GPUAvailability",
    "PLACEMENT_AGGREGATE",
    "PLACEMENT_BASIS_BY_SOURCE",
    "PLACEMENT_CAVEATS",
    "PLACEMENT_FULL_FREE_NODES",
    "PLACEMENT_REQUEST_FIT_NODES",
    "placement_basis_for_source",
]
