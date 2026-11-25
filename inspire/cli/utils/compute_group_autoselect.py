"""Helpers for auto-selecting compute groups based on browser availability."""

from __future__ import annotations

from typing import Any, Iterable, cast

from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.availability.models import GPUAvailability
from inspire.platform.web.browser_api.priority import (
    allowed_priority_levels_for_offer,
    priority_can_preempt,
    priority_matches_allowed,
)


def _gpu_type_matches(requested: str, candidate: str) -> bool:
    requested_upper = str(requested or "").strip().upper()
    candidate_upper = str(candidate or "").strip().upper()
    if requested_upper == "ANY":
        return bool(candidate_upper)
    return bool(requested_upper and candidate_upper) and (
        requested_upper in candidate_upper or candidate_upper in requested_upper
    )


def _training_request_fit_inputs(
    *,
    workspace_id: str | None,
    gpu_type: str,
    gpu_count: int,
    priority: int | None,
    offers: Iterable[Any] | None = None,
) -> tuple[
    list[dict[str, object]],
    dict[str, dict[str, int]],
    dict[str, object],
    dict[str, bool],
]:
    if offers is None:
        # Reachable only from callers that hold a workspace id; passing offers skips discovery.
        discovery = browser_api_module.list_job_offers(workspace_id=cast(str, workspace_id))
        offers = discovery.offers
        schedule: dict[str, object] = {}
        if priority is not None:
            try:
                schedule = browser_api_module.get_notebook_schedule(workspace_id=workspace_id)
            except Exception:
                schedule = {}
    else:
        schedule = {}
    candidates_by_group: dict[str, dict[str, object]] = {}
    request_specs_by_group: dict[str, dict[str, int]] = {}
    offers_by_group: dict[str, object] = {}
    preemptible_by_group: dict[str, bool] = {}

    for offer in offers:
        if int(offer.gpu_count) != gpu_count:
            continue
        if not _gpu_type_matches(gpu_type, offer.gpu_type):
            continue

        group_id = str(offer.logic_compute_group_id or "").strip()
        if not group_id:
            continue
        effective_priority = getattr(offer, "effective_priority", None)
        if effective_priority is None:
            effective_priority = priority
        evidence = getattr(offer, "priority_evidence", None)
        allowed_levels = (
            evidence.levels
            if evidence is not None
            else allowed_priority_levels_for_offer(
                schedule,
                group_id=group_id,
                quota_id=str(getattr(offer, "quota_id", "") or ""),
            )
        )
        if not priority_matches_allowed(effective_priority, allowed_levels):
            continue
        candidates_by_group[group_id] = {
            "logic_compute_group_id": group_id,
            "name": str(offer.logic_compute_group_name or group_id),
            "gpu_type": str(offer.gpu_type or gpu_type),
        }
        if group_id in offers_by_group:
            offers_by_group[group_id] = None
        else:
            offers_by_group[group_id] = offer
        if effective_priority is not None:
            preemptible_by_group[group_id] = priority_can_preempt(effective_priority)

        previous = request_specs_by_group.get(group_id, {})
        request_specs_by_group[group_id] = {
            "gpu_count": gpu_count,
            "cpu_count": max(int(offer.cpu_count), previous.get("cpu_count", 0)),
            "memory_size_gib": max(int(offer.memory_size_gib), previous.get("memory_size_gib", 0)),
        }

    return (
        list(candidates_by_group.values()),
        request_specs_by_group,
        offers_by_group,
        preemptible_by_group,
    )


def find_best_compute_group_selection(
    *,
    gpu_type: str,
    min_gpus: int,
    instance_count: int = 1,
    include_preemptible: bool = True,
    workspace_id: str | None = None,
    priority: int | None = None,
    offers: Iterable[object] | None = None,
    availability: list[GPUAvailability] | None = None,
    session: Any | None = None,
    config: Any | None = None,
) -> tuple[GPUAvailability | None, str | None, str, object | None]:
    """Select capacity and return the exact planned offer that used it."""
    candidate_groups = None
    request_specs_by_group = None
    offers_by_group: dict[str, object] = {}
    preemptible_by_group: dict[str, bool] = {}
    if workspace_id or offers is not None:
        (
            candidate_groups,
            request_specs_by_group,
            offers_by_group,
            preemptible_by_group,
        ) = _training_request_fit_inputs(
            workspace_id=workspace_id,
            gpu_type=gpu_type,
            gpu_count=min_gpus,
            priority=priority,
            offers=offers,
        )

    requires_node_fit = min_gpus > 1 or instance_count > 1
    if requires_node_fit and (session is not None or config is not None):
        from inspire.platform.web.resources import fetch_resource_availability

        # The shared selector owns placement policy. Prime its request-keyed fetch with the
        # planner's account so the selector reuses this observation instead of reopening the
        # default account's session.
        fetch_resource_availability(
            config=config,
            session=session,
            known_only=candidate_groups is None,
            workspace_ids=[workspace_id] if workspace_id else None,
            gpu_type_filter=gpu_type,
            candidate_groups=candidate_groups,
            request_specs_by_group=request_specs_by_group,
        )
    best = browser_api_module.find_best_compute_group_accurate(
        gpu_type=gpu_type,
        min_gpus=min_gpus,
        include_preemptible=include_preemptible and priority_can_preempt(priority),
        include_preemptible_by_group=preemptible_by_group,
        instance_count=instance_count,
        prefer_full_nodes=requires_node_fit,
        workspace_id=workspace_id,
        candidate_groups=candidate_groups,
        request_specs_by_group=request_specs_by_group if requires_node_fit else None,
        availability=availability,
    )
    if not best:
        return None, None, "", None

    selected_group_name = getattr(best, "group_name", "") or ""
    selected_group_id = getattr(best, "group_id", None)
    selected_offer = offers_by_group.get(str(selected_group_id or "").strip())
    return best, selected_group_id, selected_group_name, selected_offer


__all__ = ["find_best_compute_group_selection"]
