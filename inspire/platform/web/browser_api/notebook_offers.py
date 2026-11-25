"""Notebook Scheduling Offer adapter."""

from __future__ import annotations

from dataclasses import replace

from inspire.platform.web.session import WebSession

from .availability.api import gpu_type_matches
from .workload_contracts import WorkloadOfferDiscovery, discover_workload_offers


def list_notebook_offers(
    *,
    workspace_id: str,
    session: WebSession | None = None,
    gpu_type: str | None = None,
) -> WorkloadOfferDiscovery:
    discovery = discover_workload_offers(
        workload="notebook",
        workspace_id=workspace_id,
        platform_job_type="interactive_modeling",
        schedule_config_type="SCHEDULE_CONFIG_TYPE_DSW",
        session=session,
    )
    if not gpu_type:
        return discovery
    return replace(
        discovery,
        offers=[offer for offer in discovery.offers if gpu_type_matches(gpu_type, offer.gpu_type)],
    )


__all__ = ["list_notebook_offers"]
