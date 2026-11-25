"""Compute group selection logic for browser (web-session) availability APIs."""

from __future__ import annotations

from typing import Optional, TypedDict

from .api import get_accurate_gpu_availability
from .models import GPUAvailability


class _AggregateAvailabilityRequest(TypedDict, total=False):
    workspace_id: Optional[str]
    gpu_type: Optional[str]
    compute_groups: list[dict]


def find_best_compute_group_accurate(
    gpu_type: Optional[str] = None,
    min_gpus: int = 8,
    preferred_groups: Optional[list[str]] = None,
    include_preemptible: bool = True,
    instance_count: int = 1,
    prefer_full_nodes: bool = True,
    workspace_id: Optional[str] = None,
    candidate_groups: Optional[list[dict]] = None,
    request_specs_by_group: Optional[dict[str, dict[str, int]]] = None,
    include_preemptible_by_group: Optional[dict[str, bool]] = None,
    availability: Optional[list[GPUAvailability]] = None,
) -> Optional[GPUAvailability]:
    """Find the best compute group using accurate browser API data."""

    def _preemptible(group: object) -> bool:
        group_id = str(getattr(group, "group_id", "") or "")
        if include_preemptible_by_group and group_id in include_preemptible_by_group:
            return include_preemptible_by_group[group_id]
        return include_preemptible

    node_fallback_reason = ""
    require_request_fit = bool(request_specs_by_group)
    node_summary_by_workspace: dict[str, list[object]] = {}
    if prefer_full_nodes:
        try:
            from inspire.platform.web.resources import fetch_resource_availability

            node_availability = fetch_resource_availability(
                known_only=not preferred_groups and candidate_groups is None,
                workspace_ids=[workspace_id] if workspace_id else None,
                gpu_type_filter=gpu_type,
                node_summary_by_workspace=node_summary_by_workspace,
                candidate_groups=candidate_groups,
                request_specs_by_group=request_specs_by_group,
            )
            gpu_type_upper = (gpu_type or "").upper()
            required_instances = max(1, int(instance_count))

            candidates = []
            request_fit_probe_errors = []
            for group in node_availability:
                if gpu_type_upper and gpu_type_upper != "ANY":
                    if gpu_type_upper not in (group.gpu_type or "").upper():
                        continue

                if (
                    require_request_fit
                    and getattr(group, "availability_source", "") in {"aggregate", "unavailable"}
                    and group.fallback_reason
                ):
                    request_fit_probe_errors.append(group)
                    continue

                gpu_per_node = group.gpu_per_node or 0
                if gpu_per_node <= 0:
                    continue

                if require_request_fit:
                    fit_nodes = max(int(getattr(group, "request_fit_nodes", 0) or 0), 0)
                    low_priority_nodes = (
                        max(int(getattr(group, "low_priority_nodes_estimate", 0) or 0), 0)
                        if _preemptible(group)
                        else 0
                    )
                    if fit_nodes + low_priority_nodes < required_instances:
                        continue
                else:
                    required_nodes = required_instances
                    low_priority_nodes = max(
                        int(getattr(group, "low_priority_nodes_estimate", 0) or 0),
                        0,
                    )
                    effective_nodes = group.free_nodes + (
                        low_priority_nodes if _preemptible(group) else 0
                    )
                    if effective_nodes < required_nodes:
                        continue

                candidates.append(group)

            if candidates:
                if require_request_fit:
                    candidates.sort(
                        key=lambda g: (
                            int(
                                max(int(getattr(g, "request_fit_nodes", 0) or 0), 0)
                                >= required_instances
                            ),
                            max(int(getattr(g, "request_fit_nodes", 0) or 0), 0)
                            + (
                                max(
                                    int(getattr(g, "low_priority_nodes_estimate", 0) or 0),
                                    0,
                                )
                                if _preemptible(g)
                                else 0
                            ),
                            max(int(getattr(g, "request_fit_nodes", 0) or 0), 0),
                            int(getattr(g, "gpu_available_from_nodes", 0) or 0),
                            g.free_nodes,
                        ),
                        reverse=True,
                    )
                else:
                    candidates.sort(
                        key=lambda g: (
                            int(g.free_nodes >= required_nodes),
                            g.free_nodes
                            + max(
                                int(getattr(g, "low_priority_nodes_estimate", 0) or 0),
                                0,
                            ),
                            g.free_nodes,
                            g.free_gpus,
                        ),
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
                fallback_reason = getattr(selected, "fallback_reason", "")
                if require_request_fit:
                    selected_free_nodes = max(
                        int(getattr(selected, "request_fit_nodes", 0) or 0), 0
                    )
                    selected_free_gpus = int(getattr(selected, "request_fit_gpus", 0) or 0)
                    selected_low_priority_nodes = (
                        max(
                            int(getattr(selected, "low_priority_nodes_estimate", 0) or 0),
                            0,
                        )
                        if _preemptible(selected)
                        else 0
                    )
                    low_priority_gpus = selected_low_priority_nodes * selected.gpu_per_node
                    selection_source = "nodes-fit"
                    if selected_free_nodes < required_instances:
                        estimate_reason = (
                            "Selection requires low-priority nodes estimated at group level; "
                            "per-node CPU/memory fit was not verified and the launch may queue"
                        )
                        fallback_reason = "; ".join(
                            reason for reason in (fallback_reason, estimate_reason) if reason
                        )
                else:
                    selected_free_nodes = selected.free_nodes
                    selected_free_gpus = selected.free_gpus
                    low_priority_gpus = (
                        max(
                            int(getattr(selected, "low_priority_nodes_estimate", 0) or 0),
                            0,
                        )
                        * selected.gpu_per_node
                    )
                    selection_source = getattr(selected, "availability_source", "nodes")
                used_gpus = max(total_gpus - selected_free_gpus, 0)

                return GPUAvailability(
                    group_id=selected.group_id,
                    group_name=selected.group_name,
                    gpu_type=selected.gpu_type,
                    total_gpus=total_gpus,
                    used_gpus=used_gpus,
                    available_gpus=selected_free_gpus,
                    low_priority_gpus=low_priority_gpus,
                    free_nodes=selected_free_nodes,
                    gpu_per_node=selected.gpu_per_node,
                    gpu_full_free_nodes=getattr(selected, "gpu_full_free_nodes", 0),
                    schedulable_full_nodes=getattr(selected, "schedulable_full_nodes", 0),
                    partial_free_nodes=getattr(selected, "partial_free_nodes", 0),
                    low_priority_nodes_estimate=getattr(selected, "low_priority_nodes_estimate", 0),
                    selection_source=selection_source,
                    fallback_reason=fallback_reason,
                    workspace_ids=list(getattr(selected, "workspace_ids", [])),
                    workspace_aliases=list(getattr(selected, "workspace_aliases", [])),
                )
            if request_fit_probe_errors:
                # The request-fit answer is only useful when the per-node probe ran.
                # There is no aggregate fallback: report which groups are blind and
                # refuse to invent a placement from group-wide counters.
                failed_groups = ", ".join(group.group_name for group in request_fit_probe_errors)
                raise RuntimeError(f"Request-fit node capacity unavailable for: {failed_groups}")
            else:
                # Probe answered for every candidate and none could fit: that is a real
                # "no capacity" answer, not blindness -- do not paper over it with
                # aggregate numbers when the caller asked for a request-fit placement.
                if require_request_fit:
                    return None
                node_fallback_reason = "No node-level full-node candidate matched request"
        except Exception as exc:  # noqa: BLE001
            if require_request_fit:
                raise
            node_fallback_reason = f"Node-level availability failed: {exc}"

        if require_request_fit and not node_fallback_reason:
            return None

    if availability is not None:
        availability = list(availability)
    elif node_summary_by_workspace:
        availability = [
            item
            for workspace_items in node_summary_by_workspace.values()
            for item in workspace_items
            if isinstance(item, GPUAvailability)
        ]
    else:
        availability_kwargs: _AggregateAvailabilityRequest = {
            "workspace_id": workspace_id,
            "gpu_type": gpu_type,
        }
        if candidate_groups is not None:
            availability_kwargs["compute_groups"] = candidate_groups
        availability = get_accurate_gpu_availability(**availability_kwargs)
    if not availability:
        return None

    # The aggregate figure is group-wide, so it has to cover every instance: a
    # request for `min_gpus` on each of `instance_count` instances needs the product,
    # and comparing the group total against the per-instance count let a 4x8-GPU
    # request land on a group with 8 free GPUs.  The node path above is exempt - it
    # already proves `instance_count` nodes fit, and its aggregate-derived node
    # estimates are divided by the node shape, so multiplying there would count the
    # instance count twice.
    required_gpus_total = max(1, int(min_gpus)) * max(1, int(instance_count))

    def actual_available(group: GPUAvailability) -> int:
        return max(int(group.available_gpus), 0)

    def effective_available(group: GPUAvailability) -> int:
        if _preemptible(group):
            return actual_available(group) + max(int(group.low_priority_gpus or 0), 0)
        return actual_available(group)

    def _pick_best(
        candidates: list[GPUAvailability], *, allow_preemptible: bool
    ) -> GPUAvailability | None:
        if not candidates:
            return None

        if allow_preemptible:
            candidates = [
                group for group in candidates if effective_available(group) >= required_gpus_total
            ]
            sort_key = lambda group: (  # noqa: E731
                effective_available(group),
                actual_available(group),
            )
        else:
            candidates = [
                group for group in candidates if actual_available(group) >= required_gpus_total
            ]
            sort_key = lambda group: (  # noqa: E731
                actual_available(group),
                effective_available(group),
            )

        if not candidates:
            return None

        candidates.sort(key=sort_key, reverse=True)

        if preferred_groups:
            for group in candidates:
                if group.group_id in preferred_groups:
                    return group

        return candidates[0]

    if gpu_type and gpu_type.upper() != "ANY":
        gpu_type_upper = gpu_type.upper()
        filtered = [g for g in availability if gpu_type_upper in g.gpu_type.upper()]
    else:
        filtered = list(availability)

    best_availability = _pick_best(filtered, allow_preemptible=False)
    if best_availability is not None:
        if node_fallback_reason and not best_availability.fallback_reason:
            best_availability.fallback_reason = node_fallback_reason
        return best_availability

    if include_preemptible or any((include_preemptible_by_group or {}).values()):
        best_availability = _pick_best(filtered, allow_preemptible=True)
        if best_availability is not None:
            if node_fallback_reason and not best_availability.fallback_reason:
                best_availability.fallback_reason = node_fallback_reason
            return best_availability

    return None


__all__ = ["find_best_compute_group_accurate"]
