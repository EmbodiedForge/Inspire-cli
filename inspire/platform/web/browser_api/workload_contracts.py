"""Shared primitives for workload-specific platform contract adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Any, Callable

from inspire.platform.web.browser_api.availability.api import (
    get_low_priority_gpu_usage,
    list_compute_groups,
)
from inspire.platform.web.browser_api.core import _get_base_url
from inspire.platform.web.browser_api.notebooks import get_notebook_schedule, get_resource_prices
from inspire.platform.web.browser_api.priority import allowed_priority_levels_for_offer
from inspire.platform.web.session import WebSession, fetch_node_dimensions, get_web_session


@dataclass(frozen=True)
class WorkloadOffer:
    """One launchable compute-group and quota-row combination."""

    workspace_id: str
    logic_compute_group_id: str
    logic_compute_group_name: str
    quota_id: str
    cpu_count: int
    memory_size_gib: int
    gpu_count: int
    raw: dict[str, Any] = field(repr=False)
    gpu_type: str = ""
    allowed_priority_levels: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkloadOfferDiscovery:
    """Effective offers, their schedule evidence, and explicit coverage gaps."""

    workload: str
    workspace_id: str
    schedule_config_type: str
    offers: list[WorkloadOffer]
    capability_groups_without_quota_rows: list[dict[str, str]]
    quota_rows_without_capability: list[dict[str, Any]]
    failures: list[dict[str, str]] = field(default_factory=list)
    schedule_error: str | None = None
    queried_group_ids: list[str] = field(default_factory=list)
    schedule: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class WorkloadAvailability:
    """Volatile aggregate capacity for one workload-capable compute group."""

    workload: str
    workspace_id: str
    logic_compute_group_id: str
    logic_compute_group_name: str
    launchable_offer_count: int
    cpu_total: int | None
    cpu_used: int | None
    cpu_available: int | None
    gpu_total: int | None
    gpu_used: int | None
    gpu_available: int | None
    gpu_low_priority_used: int | None
    memory_gib_total: float | None
    memory_gib_used: float | None
    memory_gib_available: float | None
    low_priority_source: str | None = None
    source: str = "aggregate_compute_resources"
    error: str | None = None


def parse_support_job_types(value: Any) -> set[str]:
    """Normalize the platform's string-or-list support declaration."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return set()
    if not isinstance(value, list):
        return set()
    return {str(item or "").strip().lower() for item in value if str(item or "").strip()}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_number(value: Any, convert):  # noqa: ANN001, ANN202
    if value is None:
        return None
    try:
        return convert(value)
    except (TypeError, ValueError):
        return None


def _gpu_type(row: dict[str, Any]) -> str:
    gpu_info = row.get("gpu_info") or {}
    return str(
        row.get("gpu_type")
        or gpu_info.get("gpu_type_display")
        or gpu_info.get("gpu_product_simple")
        or gpu_info.get("gpu_type")
        or ""
    )


def discover_workload_offers(
    *,
    workload: str,
    workspace_id: str,
    platform_job_type: str | Iterable[str],
    schedule_config_type: str,
    session: WebSession | None = None,
    group_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> WorkloadOfferDiscovery:
    """Resolve offers within the requested groups, retaining every coverage gap."""
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("workspace_id is required")
    if session is None:
        session = get_web_session()

    groups = list_compute_groups(workspace_id=workspace_id, session=session)
    requested_job_types = (
        {platform_job_type.strip().lower()}
        if isinstance(platform_job_type, str)
        else {str(item or "").strip().lower() for item in platform_job_type}
    )
    requested_job_types.discard("")
    offers: list[WorkloadOffer] = []
    capability_without_quota: list[dict[str, str]] = []
    quota_without_capability: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    queried_group_ids: list[str] = []
    schedule: dict[str, Any] = {}
    schedule_error: str | None = None
    if schedule_config_type == "SCHEDULE_CONFIG_TYPE_TRAIN":
        try:
            schedule = get_notebook_schedule(
                workspace_id=workspace_id, session=session, strict=True
            )
        except Exception as exc:  # noqa: BLE001 - unknown evidence is reported, not hidden
            schedule_error = str(exc)

    for group in groups:
        if not isinstance(group, dict):
            continue
        if group_filter is not None and not group_filter(group):
            continue
        group_id = str(group.get("logic_compute_group_id") or group.get("id") or "").strip()
        if not group_id:
            continue
        queried_group_ids.append(group_id)
        group_name = str(group.get("logic_compute_group_name") or group.get("name") or group_id)
        supports_workload = group.get("support_job_type_list") is None or bool(
            requested_job_types & parse_support_job_types(group.get("support_job_type_list"))
        )
        try:
            rows = get_resource_prices(
                workspace_id=workspace_id,
                logic_compute_group_id=group_id,
                schedule_config_type=schedule_config_type,
                strict=True,
                session=session,
            )
        except Exception as exc:  # noqa: BLE001 - other groups remain useful evidence
            failures.append(
                {
                    "stage": "resource_prices",
                    "logic_compute_group_id": group_id,
                    "logic_compute_group_name": group_name,
                    "error": str(exc),
                }
            )
            continue
        dict_rows = [row for row in rows if isinstance(row, dict)]

        if supports_workload and not dict_rows:
            capability_without_quota.append(
                {
                    "logic_compute_group_id": group_id,
                    "logic_compute_group_name": group_name,
                }
            )
        if dict_rows and not supports_workload:
            quota_without_capability.append(
                {
                    "logic_compute_group_id": group_id,
                    "logic_compute_group_name": group_name,
                    "row_count": len(dict_rows),
                }
            )
        if not supports_workload:
            continue

        for row in dict_rows:
            quota_id = str(row.get("quota_id") or "")
            allowed_levels = allowed_priority_levels_for_offer(
                schedule,
                group_id=group_id,
                quota_id=quota_id,
            )
            raw = dict(row)
            if allowed_levels:
                raw["allowed_priority_levels"] = list(allowed_levels)
            offers.append(
                WorkloadOffer(
                    workspace_id=workspace_id,
                    logic_compute_group_id=group_id,
                    logic_compute_group_name=group_name,
                    quota_id=quota_id,
                    cpu_count=_as_int(row.get("cpu_count")),
                    memory_size_gib=_as_int(row.get("memory_size_gib")),
                    gpu_count=_as_int(row.get("gpu_count")),
                    raw=raw,
                    gpu_type=_gpu_type(row),
                    allowed_priority_levels=allowed_levels,
                )
            )

    return WorkloadOfferDiscovery(
        workload=workload,
        workspace_id=workspace_id,
        schedule_config_type=schedule_config_type,
        offers=offers,
        capability_groups_without_quota_rows=capability_without_quota,
        quota_rows_without_capability=quota_without_capability,
        failures=failures,
        schedule_error=schedule_error,
        queried_group_ids=queried_group_ids,
        schedule=schedule,
    )


def get_workload_availability(
    discovery: WorkloadOfferDiscovery,
    *,
    session: WebSession | None = None,
) -> list[WorkloadAvailability]:
    """Fetch advisory node-derived capacity for groups relevant to one offer discovery."""
    if session is None:
        session = get_web_session()
    groups: dict[str, str] = {}
    offer_counts: dict[str, int] = {}
    for offer in discovery.offers:
        groups[offer.logic_compute_group_id] = offer.logic_compute_group_name
        offer_counts[offer.logic_compute_group_id] = (
            offer_counts.get(offer.logic_compute_group_id, 0) + 1
        )
    for group in discovery.capability_groups_without_quota_rows:
        group_id = str(group.get("logic_compute_group_id") or "").strip()
        if group_id:
            groups.setdefault(group_id, str(group.get("logic_compute_group_name") or group_id))

    results: list[WorkloadAvailability] = []
    for group_id, group_name in groups.items():
        try:
            payload = fetch_node_dimensions(
                session,
                base_url=_get_base_url(),
                workspace_id=discovery.workspace_id,
                logic_compute_group_id=group_id,
            )
            nodes = list((payload.get("data") or {}).get("node_dimensions") or [])
            from inspire.platform.web.resources import node_is_schedulable

            ready = [node for node in nodes if node_is_schedulable(node)]
            cpu_total = sum(int((node.get("cpu") or {}).get("total") or 0) for node in ready)
            cpu_used = sum(int((node.get("cpu") or {}).get("used") or 0) for node in ready)
            gpu_total = sum(int((node.get("gpu") or {}).get("total") or 0) for node in ready)
            gpu_used = sum(int((node.get("gpu") or {}).get("used") or 0) for node in ready)
            gpu_available = sum(
                max(int((node.get("gpu") or {}).get("available") or 0), 0) for node in ready
            )
            memory_total = sum(
                float((node.get("memory") or {}).get("total") or 0) for node in ready
            )
            memory_used = sum(float((node.get("memory") or {}).get("used") or 0) for node in ready)
            low_priority: int | None = None
            try:
                low_priority = get_low_priority_gpu_usage(
                    workspace_id=discovery.workspace_id,
                    logic_compute_group_id=group_id,
                    session=session,
                )
            except Exception:  # noqa: BLE001 - advisory only
                low_priority = None
            results.append(
                WorkloadAvailability(
                    workload=discovery.workload,
                    workspace_id=discovery.workspace_id,
                    logic_compute_group_id=group_id,
                    logic_compute_group_name=group_name,
                    launchable_offer_count=offer_counts.get(group_id, 0),
                    cpu_total=cpu_total,
                    cpu_used=cpu_used,
                    cpu_available=max(cpu_total - cpu_used, 0),
                    gpu_total=gpu_total,
                    gpu_used=gpu_used,
                    gpu_available=gpu_available,
                    gpu_low_priority_used=low_priority,
                    low_priority_source=(
                        "aggregate_advisory" if low_priority is not None else None
                    ),
                    memory_gib_total=memory_total,
                    memory_gib_used=memory_used,
                    memory_gib_available=max(memory_total - memory_used, 0.0),
                    source="node_dimensions",
                )
            )
        except Exception as exc:  # noqa: BLE001 - advisory failures remain visible
            results.append(
                WorkloadAvailability(
                    workload=discovery.workload,
                    workspace_id=discovery.workspace_id,
                    logic_compute_group_id=group_id,
                    logic_compute_group_name=group_name,
                    launchable_offer_count=offer_counts.get(group_id, 0),
                    cpu_total=None,
                    cpu_used=None,
                    cpu_available=None,
                    gpu_total=None,
                    gpu_used=None,
                    gpu_available=None,
                    gpu_low_priority_used=None,
                    memory_gib_total=None,
                    memory_gib_used=None,
                    memory_gib_available=None,
                    source="unavailable",
                    error=str(exc),
                )
            )
    return results


__all__ = [
    "WorkloadOffer",
    "WorkloadOfferDiscovery",
    "WorkloadAvailability",
    "discover_workload_offers",
    "get_workload_availability",
    "parse_support_job_types",
]
