"""Distributed training Scheduling Offer adapter."""

from __future__ import annotations

from collections.abc import Callable

from inspire.platform.web.session import WebSession

from .workload_contracts import WorkloadOfferDiscovery, discover_workload_offers


def list_job_offers(
    *,
    workspace_id: str,
    session: WebSession | None = None,
    group: str | None = None,
    exact_group: bool = False,
    group_filter: Callable[[dict], bool] | None = None,
) -> WorkloadOfferDiscovery:
    needle = str(group or "").strip().lower()

    def matches(item: dict) -> bool:
        if group_filter is not None and not group_filter(item):
            return False
        if not needle:
            return True
        group_id = str(item.get("logic_compute_group_id") or item.get("id") or "")
        group_name = str(item.get("logic_compute_group_name") or item.get("name") or "")
        if needle == group_id.lower():
            return True
        return needle == group_name.lower() if exact_group else needle in group_name.lower()

    return discover_workload_offers(
        workload="job",
        workspace_id=workspace_id,
        platform_job_type="distributed_training",
        schedule_config_type="SCHEDULE_CONFIG_TYPE_TRAIN",
        session=session,
        group_filter=matches,
    )


__all__ = ["list_job_offers"]
