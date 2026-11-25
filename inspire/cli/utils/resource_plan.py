"""One workspace-scoped Job resource plan shared by queries and submission."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from typing import Any, Iterable, cast

from inspire.cli.utils import job_submit
from inspire.cli.utils.compute_group_autoselect import find_best_compute_group_selection
from inspire.cli.utils.training_resources import parse_quota_spec
from inspire.platform.openapi.resources import parse_resource_request
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import workspaces as workspace_api_module
from inspire.platform.web.browser_api.availability.models import GPUAvailability
from inspire.platform.web.browser_api.priority import (
    OfferPriorityEvidence,
    priority_can_preempt,
    priority_evidence_for_offer,
    priority_matches_allowed,
)
from inspire.platform.web.browser_api.workload_contracts import (
    WorkloadAvailability,
    WorkloadOffer,
    WorkloadOfferDiscovery,
    get_workload_availability,
)
from inspire.platform.web import session as web_session_module
from inspire.platform.web.session import WebSession


def _gpu_type_matches(requested: str, candidate: str) -> bool:
    left = str(requested or "").strip().upper()
    right = str(candidate or "").strip().upper()
    if left == "ANY":
        return bool(right)
    return bool(left and right) and (left in right or right in left)


def _selector(offer: WorkloadOffer) -> str:
    return (
        f"--group '{offer.logic_compute_group_id}' --quota "
        f"{offer.gpu_count},{offer.cpu_count},{offer.memory_size_gib}"
    )


@dataclass(frozen=True)
class PlannedOffer:
    offer: WorkloadOffer
    priority_evidence: OfferPriorityEvidence
    priority_plan: job_submit.PriorityPlan | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.offer, name)

    @property
    def effective_priority(self) -> int | None:
        return self.priority_plan.priority if self.priority_plan is not None else None

    @property
    def selector(self) -> str:
        return _selector(self.offer)

    def as_dict(self, requested_priority: int | None = None) -> dict[str, Any]:
        effective = self.effective_priority
        return {
            **asdict(self.offer),
            "selector": self.selector,
            "priority_evidence": asdict(self.priority_evidence),
            "priority_requested": requested_priority,
            "priority_effective": effective,
            "priority_ceiling": (
                asdict(self.priority_plan.ceiling)
                if self.priority_plan is not None and self.priority_plan.ceiling is not None
                else None
            ),
            "priority_warning": (
                self.priority_plan.warning if self.priority_plan is not None else None
            ),
            "preemptible_usable": (
                priority_can_preempt(effective) if effective is not None else None
            ),
        }


@dataclass(frozen=True)
class JobResourcePlan:
    workspace_id: str
    offers: tuple[PlannedOffer, ...]
    discovery: WorkloadOfferDiscovery = field(repr=False, compare=False)
    requested_priority: int | None = None
    group: str | None = None
    location: str | None = None
    resource: str | None = None
    quota: str | None = None
    observed_at: str = ""
    capacity_requested: bool = False
    availability: tuple[WorkloadAvailability, ...] = ()
    failures: tuple[dict[str, str], ...] = ()
    session: WebSession | None = field(default=None, repr=False, compare=False)
    config: Any | None = field(default=None, repr=False, compare=False)

    @property
    def coverage(self) -> dict[str, Any]:
        price_failures = [item for item in self.failures if item.get("stage") == "resource_prices"]
        failed_ids = {item.get("logic_compute_group_id") for item in price_failures}
        queried = set(self.discovery.queried_group_ids)
        capacity_errors = [item for item in self.availability if item.error]
        return {
            "groups_requested": len(queried),
            "groups_with_price_data": len(queried - failed_ids),
            "groups_with_price_failures": len(price_failures),
            "capacity_requested": self.capacity_requested,
            "capacity_groups_observed": len(self.availability) - len(capacity_errors),
            "capacity_groups_failed": len(capacity_errors),
            "schedule": "unavailable" if self.discovery.schedule_error else "observed",
            "complete": not self.failures and not capacity_errors,
        }

    @property
    def warnings(self) -> list[str]:
        warnings = [
            f"{item.get('stage', 'platform')} failed for "
            f"{item.get('logic_compute_group_name') or item.get('logic_compute_group_id') or 'workspace'}: "
            f"{item.get('error', 'unknown error')}"
            for item in self.failures
        ]
        warnings.extend(
            f"capacity failed for {item.logic_compute_group_name}: {item.error}"
            for item in self.availability
            if item.error
        )
        return list(dict.fromkeys(warnings))

    def gpu_snapshots(self) -> list[GPUAvailability]:
        snapshots: list[GPUAvailability] = []
        for item in self.availability:
            if item.error or item.gpu_available is None:
                continue
            available = max(item.gpu_available, 0)
            used = item.gpu_used
            total = item.gpu_total
            if used is None and total is not None:
                used = max(total - available, 0)
            if total is None:
                total = (used or 0) + available
            offer = next(
                (
                    planned.offer
                    for planned in self.offers
                    if planned.logic_compute_group_id == item.logic_compute_group_id
                ),
                None,
            )
            snapshots.append(
                GPUAvailability(
                    group_id=item.logic_compute_group_id,
                    group_name=item.logic_compute_group_name,
                    gpu_type=offer.gpu_type if offer is not None else "",
                    total_gpus=total,
                    used_gpus=used or 0,
                    available_gpus=available,
                    low_priority_gpus=cast(
                        Any,
                        (
                            max(item.gpu_low_priority_used, 0)
                            if item.gpu_low_priority_used is not None
                            else None
                        ),
                    ),
                    low_priority_source=getattr(item, "low_priority_source", None),
                    selection_source="nodes",
                    fallback_reason="",
                    workspace_ids=[self.workspace_id],
                )
            )
        return snapshots

    def as_dict(self, *, include_offers: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "job_resource_plan",
            "workload": "job",
            "workspace_id": self.workspace_id,
            "scope": {
                "workspace_id": self.workspace_id,
                "group": self.group,
                "location": self.location,
                "resource": self.resource,
                "quota": self.quota,
                "priority": self.requested_priority,
            },
            "observed_at": self.observed_at,
            "coverage": self.coverage,
            "failures": list(self.failures),
            "warnings": self.warnings,
            "capability_groups_without_quota_rows": (
                self.discovery.capability_groups_without_quota_rows
            ),
            "quota_rows_without_capability": self.discovery.quota_rows_without_capability,
        }
        if include_offers:
            payload["offers"] = [
                planned.as_dict(self.requested_priority) for planned in self.offers
            ]
        if self.availability:
            payload["signals"] = [asdict(item) for item in self.availability]
        return payload


@dataclass(frozen=True)
class JobOfferSelection:
    planned: PlannedOffer
    availability: GPUAvailability | None = None

    @property
    def offer(self) -> WorkloadOffer:
        return self.planned.offer


def _is_fair_workspace(workspace_id: str, session: WebSession) -> bool:
    workspaces = workspace_api_module.try_enumerate_workspaces(
        session,
        workspace_id=workspace_id,
        base_url=str(getattr(session, "base_url", None) or ""),
        strict=True,
    )
    selected = next(
        (
            item
            for item in workspaces
            if str(item.get("id") or "").strip() == str(workspace_id or "").strip()
        ),
        None,
    )
    if selected is None:
        raise ValueError(
            f"Cannot determine whether workspace {workspace_id} uses fair scheduling: "
            "the live workspace list does not include it. Retry rather than assuming a "
            "non-fair policy."
        )
    return bool(selected.get("is_fair_workspace"))


def build_job_resource_plan(
    *,
    workspace_id: str,
    session: WebSession | None = None,
    group: str | None = None,
    exact_group: bool = False,
    location: str | None = None,
    config: Any | None = None,
    resource: str | None = None,
    quota: str | None = None,
    priority: int | None = None,
    priority_from_caller: bool = False,
    include_capacity: bool = False,
    is_fair: bool | None = None,
    discovery: WorkloadOfferDiscovery | None = None,
) -> JobResourcePlan:
    if discovery is None:
        session = session or web_session_module.get_web_session(
            account=getattr(config, "account_alias", None)
        )
        group_filter = None
        if location:
            from inspire.cli.utils.training_resources import _group_matches_location

            if config is None:
                raise ValueError("A config is required to resolve a location")
            group_filter = partial(_group_matches_location, config, location=location)
        elif group and config is not None:
            from inspire.cli.utils.training_resources import (
                _group_matches_exact,
                _group_matches_location,
            )

            matches = _group_matches_exact if exact_group else _group_matches_location
            group_filter = partial(matches, config, location=group)
        discovery = browser_api_module.list_job_offers(
            workspace_id=workspace_id,
            session=session,
            group=group if group_filter is None else None,
            exact_group=exact_group,
            group_filter=group_filter,
        )
    failures = list(discovery.failures)
    if discovery.schedule_error:
        failures.append(
            {
                "stage": "schedule",
                "logic_compute_group_id": "",
                "logic_compute_group_name": "workspace",
                "error": discovery.schedule_error,
            }
        )
    if (group or location) and not discovery.queried_group_ids:
        raise ValueError(
            f"Compute group or location {(group or location)!r} was not found in "
            f"workspace {workspace_id}"
        )
    price_failures = [item for item in failures if item.get("stage") == "resource_prices"]
    if price_failures and (group or location or not discovery.offers):
        details = "; ".join(item.get("error", "unknown error") for item in price_failures)
        raise RuntimeError(f"Scheduling Offer discovery was not observable: {details}")

    requested_gpu_type = None
    requested_gpu_count = None
    if resource:
        requested_gpu_type, requested_gpu_count = parse_resource_request(resource)
    quota_tuple = parse_quota_spec(quota) if quota else None
    offers = [
        offer
        for offer in discovery.offers
        if (
            requested_gpu_type is None
            or (
                offer.gpu_count == requested_gpu_count
                and _gpu_type_matches(requested_gpu_type.value, offer.gpu_type)
            )
        )
        and (
            quota_tuple is None
            or (offer.gpu_count, offer.cpu_count, offer.memory_size_gib) == quota_tuple
        )
    ]
    if (resource or quota) and not offers:
        raise ValueError(
            "No Scheduling Offer matches the requested resource and quota in "
            f"workspace {workspace_id}; run `inspire job offers --workspace-id {workspace_id}` "
            "and paste one row's --group/--quota selector."
        )

    if priority is not None and is_fair is None:
        session = session or web_session_module.get_web_session(
            account=getattr(config, "account_alias", None)
        )
        is_fair = _is_fair_workspace(workspace_id, session)
    planned_offers = []
    for offer in offers:
        evidence = priority_evidence_for_offer(
            discovery.schedule,
            group_id=offer.logic_compute_group_id,
            quota_id=offer.quota_id,
            gpu_count=offer.gpu_count,
            cpu_count=offer.cpu_count,
            memory_size_gib=offer.memory_size_gib,
        )
        priority_plan = (
            job_submit.priority_plan_for_offer(
                workspace_id=workspace_id,
                group_id=offer.logic_compute_group_id,
                quota_id=offer.quota_id,
                priority=priority,
                priority_from_caller=priority_from_caller,
                is_fair=bool(is_fair),
                evidence=evidence,
            )
            if priority is not None
            else None
        )
        planned_offers.append(
            PlannedOffer(offer=offer, priority_evidence=evidence, priority_plan=priority_plan)
        )

    availability: list[WorkloadAvailability] = []
    if include_capacity:
        session = session or web_session_module.get_web_session(
            account=getattr(config, "account_alias", None)
        )
        availability = get_workload_availability(discovery, session=session)
    return JobResourcePlan(
        workspace_id=workspace_id,
        offers=tuple(planned_offers),
        discovery=discovery,
        requested_priority=priority,
        group=group,
        location=location,
        resource=resource,
        quota=quota,
        observed_at=datetime.now(timezone.utc).isoformat(),
        capacity_requested=include_capacity,
        availability=tuple(availability),
        failures=tuple(failures),
        session=session,
        config=config,
    )


def _eligible_offers(plan: JobResourcePlan) -> list[PlannedOffer]:
    return [
        planned
        for planned in plan.offers
        if planned.effective_priority is None
        or priority_matches_allowed(planned.effective_priority, planned.priority_evidence.levels)
    ]


def select_job_offer(
    plan: JobResourcePlan,
    *,
    gpu_type: str,
    gpu_count: int,
    nodes: int,
    auto: bool,
) -> JobOfferSelection:
    eligible = [
        planned
        for planned in _eligible_offers(plan)
        if planned.gpu_count == gpu_count and _gpu_type_matches(gpu_type, planned.gpu_type)
    ]
    if not eligible:
        raise ValueError(
            "Scheduling Offers exist for this resource, but none has unambiguous evidence "
            f"for priority {plan.requested_priority}; choose another offer or priority."
        )
    if not auto:
        if len(eligible) != 1:
            candidates = "; ".join(planned.selector for planned in eligible)
            raise ValueError(
                "Ambiguous train Scheduling Offer; pass an exact --group and --quota. "
                f"Candidates: {candidates}"
            )
        return JobOfferSelection(planned=eligible[0])

    by_group: dict[str, list[PlannedOffer]] = {}
    for planned in eligible:
        by_group.setdefault(planned.logic_compute_group_id, []).append(planned)
    for group_id, group_offers in by_group.items():
        if len({planned.effective_priority for planned in group_offers}) > 1:
            raise ValueError(
                f"Offers in group {group_id} resolve to different effective priorities; "
                "pass an exact --quota instead of inferring one capacity policy."
            )

    eligible_group_ids = {planned.logic_compute_group_id for planned in eligible}
    selection_snapshots = [
        replace(snapshot, low_priority_gpus=0) if snapshot.low_priority_gpus is None else snapshot
        for snapshot in plan.gpu_snapshots()
        if snapshot.group_id in eligible_group_ids
    ]
    best, _group_id, _group_name, selected = find_best_compute_group_selection(
        gpu_type=gpu_type,
        min_gpus=gpu_count,
        instance_count=nodes,
        workspace_id=plan.workspace_id,
        offers=eligible,
        availability=selection_snapshots,
        session=plan.session,
        config=plan.config,
    )
    if best is not None:
        if not isinstance(selected, PlannedOffer):
            raise ValueError(
                "Multiple quota rows in the selected group match; pass an exact --quota."
            )
        return JobOfferSelection(planned=selected, availability=best)

    if any(len(group_offers) > 1 for group_offers in by_group.values()):
        raise ValueError(
            "Multiple quota rows in one group match; pass an exact --group and --quota."
        )
    fallback = sorted(eligible, key=lambda planned: planned.logic_compute_group_id)[0]
    return JobOfferSelection(planned=fallback)


def offer_candidates(offers: Iterable[PlannedOffer]) -> str:
    return "; ".join(planned.selector for planned in offers)


__all__ = [
    "JobOfferSelection",
    "JobResourcePlan",
    "PlannedOffer",
    "build_job_resource_plan",
    "offer_candidates",
    "select_job_offer",
]
