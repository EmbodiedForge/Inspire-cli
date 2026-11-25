"""Model Serving Scheduling Offer adapter."""

from __future__ import annotations

from inspire.platform.web.session import WebSession

from .workload_contracts import WorkloadOfferDiscovery, discover_workload_offers


def list_model_offers(
    *, workspace_id: str, session: WebSession | None = None
) -> WorkloadOfferDiscovery:
    return discover_workload_offers(
        workload="model",
        workspace_id=workspace_id,
        platform_job_type={"inference_serving_customize", "inference_serving_exclusive"},
        schedule_config_type="SCHEDULE_CONFIG_TYPE_SERVE",
        session=session,
    )


__all__ = ["list_model_offers"]
