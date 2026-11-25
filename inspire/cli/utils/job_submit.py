"""Shared helpers for submitting training jobs through web-session browser API."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import workspaces as workspace_api_module
from inspire.platform.web.browser_api import JobInfo, ProjectInfo
from inspire.platform.web.session import WebSession
from inspire.config import (
    Config,
    ConfigError,
    build_runtime_activate_command,
    build_runtime_cd_command,
    build_runtime_export_commands,
    load_runtime_session_state_for_config,
    resolve_effective_remote_runtime,
)
from inspire.config.projects import resolve_project_request
from inspire.cli.utils.datasets import validation_pair_key
from inspire.cli.utils.priority_observations import (
    observed_accepted_priority,
    record_accepted_priority,
)
from inspire.cli.utils.training_resources import (
    TrainingResourceSelection,
    resolve_training_resource_selection,
    training_resource_selection_from_offer,
)
from inspire.platform.web.browser_api.priority import (
    PREEMPT_MIN_PRIORITY,
    PRIORITY_LEVEL_BANDS,
    OfferPriorityEvidence,
    highest_declared_priority,
    normalize_priority_levels,
    priority_evidence_for_offer,
)

DEFAULT_JOB_IMAGE_PATH = "inspire-studio/ngc-cuda12.8-base:1.0"
DEFAULT_JOB_IMAGE = f"docker.example.com/{DEFAULT_JOB_IMAGE_PATH}"
DEFAULT_JOB_IMAGE_TYPE = "SOURCE_PRIVATE"

#: Shared-memory default for a job when neither --shm nor config/env supplies one. 200 GiB matches
#: the OpenAPI create client; it is capped to the selected quota's memory so a small CPU row does
#: not receive a larger /dev/shm than its whole memory budget.
DEFAULT_JOB_SHM_SIZE = 200

logger = logging.getLogger(__name__)

#: A negotiated-priority submit never makes more create calls than this, whatever the platform keeps
#: refusing: the capped priority first, then the one step down a refusal earns
#: (:func:`_priority_fallbacks` hands back a single value). An explicit uncapped priority gets only its
#: initial attempt. The bound stops retries on every number the platform has ever refused.
MAX_PRIORITY_ATTEMPTS = 3

#: The ``task_priority`` range the create endpoint accepts at all, whatever one workspace takes:
#: the floor of the lowest band to the ceiling of the highest.
_TASK_PRIORITY_MIN = min(band[0] for band in PRIORITY_LEVEL_BANDS.values())
_TASK_PRIORITY_MAX = max(band[1] for band in PRIORITY_LEVEL_BANDS.values())

_PRIORITY_REFUSAL_CODE = re.compile(r"\bInvalidParameter\b")
_PRIORITY_REFUSAL_WORDING = re.compile(r"task_priority|优先级|priority", re.IGNORECASE)


@dataclass(frozen=True)
class JobSubmission:
    job_id: Optional[str]
    data: dict
    result: Any
    wrapped_command: str
    max_time_ms: str
    reconciled: bool = False
    #: The ``task_priority`` the platform actually took, which differs from the prepared
    #: payload's whenever the CLI had to fall back to another value.
    priority: int | None = None
    #: One-line, post-submit statement of what the platform refused and what it accepted.
    priority_note: str | None = None


@dataclass(frozen=True)
class PreparedJobLaunch:
    """Invocation-local, read-only result of preparing one job launch."""

    payload: dict[str, Any]
    wrapped_command: str
    max_time_ms: str
    resource: TrainingResourceSelection
    image: ResolvedJobImage
    warnings: tuple[str, ...] = ()
    #: Lower priorities supported by this exact offer's declarations or accepted history.
    priority_candidates: tuple[int, ...] = ()
    #: The priority the caller or configuration asked for, before the CLI capped it.
    priority_requested: int | None = None
    #: Whether the submitted number is the caller's own. Capping is the CLI changing it, so a
    #: capped value — however it arrived — is one the CLI may answer a refusal for.
    priority_from_caller: bool = True
    #: The selected offer's priority ceiling when one could be derived.
    priority_ceiling: PriorityCeiling | None = None


@dataclass(frozen=True)
class PriorityCeiling:
    """The highest ``task_priority`` supported by one Scheduling Offer's evidence.

    Only explicit declarations produce a ceiling during planning. An accepted value is
    fallback evidence after a platform refusal, not proof of an upper bound.
    """

    value: int
    source: str
    levels: tuple[str, ...] = ()

    def top_levels(self) -> tuple[str, ...]:
        """The declared levels whose band tops out at this ceiling's value."""
        return tuple(level for level in self.levels if PRIORITY_LEVEL_BANDS[level][1] == self.value)

    def source_phrase(self) -> str:
        """Where the ceiling comes from, without repeating its value."""
        if self.source == "declared":
            return f"its Scheduling Offers declare priority level {' / '.join(self.top_levels())}"
        return "it is the highest priority this CLI has watched this Scheduling Offer accept"

    def evidence_phrase(self) -> str:
        """Where this ceiling comes from, in the words the CLI says out loud."""
        if self.source == "declared":
            return f"{self.source_phrase()}, whose highest priority is {self.value}"
        return (
            f"the highest priority this CLI has seen this Scheduling Offer accept is {self.value}"
        )


@dataclass(frozen=True)
class PriorityPlan:
    """What to submit as ``task_priority`` and what the CLI may answer a refusal with.

    ``priority`` is capped only to the selected offer's ceiling. A refusal of the caller's
    own uncapped number is reported, never worked around.
    """

    priority: int
    warning: str | None = None
    candidates: tuple[int, ...] = ()
    from_caller: bool = True
    ceiling: PriorityCeiling | None = None


@dataclass(frozen=True)
class ResolvedJobImage:
    image: str
    image_type: str


def resolve_fault_tolerance(
    project: ProjectInfo,
    fault_tolerant: bool | None,
    *,
    task_priority: int | None = None,
) -> tuple[bool, bool]:
    """Determine low-priority status and supported fault-tolerance intent.

    Returns (is_low_priority, auto_fault_tolerance).
    """
    project_is_low = (project.priority_level or "").upper() == "LOW"
    task_is_low = task_priority is not None and task_priority < PREEMPT_MIN_PRIORITY
    is_low_priority = project_is_low or task_is_low
    if fault_tolerant is None and is_low_priority:
        fault_tolerant = True
    return is_low_priority, bool(fault_tolerant)


def _priority_label(priority: int) -> str:
    """The band name the platform itself puts on a numeric task priority."""
    if priority <= 3:
        return "LOW, preemptible"
    if priority <= 6:
        return "NORMAL"
    return "HIGH"


def declared_priority_ceiling(levels: Any) -> PriorityCeiling | None:
    """The highest ``task_priority`` a set of declared priority levels takes.

    The band arithmetic is :func:`highest_declared_priority`, the one place a level's band is
    turned into a number — ``low`` alone gives 3, ``normal`` gives 6, ``high`` gives 10, and
    ``low`` together with ``normal`` gives 6. Every declared level travels with the number,
    because a cap the CLI cannot say the source of is a guess.
    """
    top = highest_declared_priority(levels)
    if top is None:
        return None
    return PriorityCeiling(
        value=top, source="declared", levels=tuple(sorted(set(normalize_priority_levels(levels))))
    )


def _declared_priority_values(levels: Any) -> tuple[int, ...]:
    """Every band bound the declaration itself states, strongest first.

    These are the numbers the selected Scheduling Offer vouches for, which is what a refusal
    of the ceiling may be answered with.
    """
    bounds = {
        bound
        for level in set(normalize_priority_levels(levels))
        for bound in PRIORITY_LEVEL_BANDS[level]
    }
    return tuple(sorted(bounds, reverse=True))


def _priority_cap_warning(
    *,
    workspace_id: str,
    group_id: str,
    quota_id: str,
    requested: int,
    submitted: int,
    ceiling: PriorityCeiling,
) -> str:
    return (
        f"Priority {requested} is above the ceiling of Scheduling Offer {group_id}/{quota_id} "
        f"in fair-scheduling workspace {workspace_id}: {ceiling.evidence_phrase()}, so the job "
        f"is submitted at priority {submitted} ({_priority_label(submitted)})."
    )


def _priority_fallbacks(
    ceiling: PriorityCeiling | None,
    *,
    submitted: int,
    workspace_id: str,
    group_id: str = "",
    quota_id: str = "",
) -> tuple[int, ...]:
    """Return one lower value supported by this exact offer's evidence."""
    evidence: set[int] = set()
    if ceiling is not None and ceiling.source == "declared":
        evidence.update(_declared_priority_values(ceiling.levels))
    learned = observed_accepted_priority(workspace_id, group_id=group_id, quota_id=quota_id)
    if learned is not None and _TASK_PRIORITY_MIN <= learned <= _TASK_PRIORITY_MAX:
        evidence.add(learned)
    top = ceiling.value if ceiling is not None else _TASK_PRIORITY_MAX
    lower = [
        value for value in evidence if _TASK_PRIORITY_MIN <= value <= top and value < submitted
    ]
    return tuple(sorted(lower, reverse=True)[:1])


def priority_plan_for_offer(
    *,
    workspace_id: str,
    group_id: str,
    quota_id: str,
    priority: int,
    priority_from_caller: bool,
    is_fair: bool = True,
    schedule: dict[str, Any] | None = None,
    evidence: OfferPriorityEvidence | None = None,
    gpu_count: int | None = None,
    cpu_count: int | None = None,
    memory_size_gib: int | None = None,
) -> PriorityPlan:
    """Resolve priority only from declarations and observations for the selected offer."""
    if not is_fair:
        return PriorityPlan(priority=priority, from_caller=priority_from_caller)
    if evidence is None:
        evidence = priority_evidence_for_offer(
            schedule or {},
            group_id=group_id,
            quota_id=quota_id,
            gpu_count=gpu_count,
            cpu_count=cpu_count,
            memory_size_gib=memory_size_gib,
        )
    if evidence.status == "conflict":
        return PriorityPlan(
            priority=priority,
            warning=(
                f"Priority evidence for Scheduling Offer {group_id}/{quota_id} conflicts across "
                f"declarations {', '.join(evidence.declaration_ids)}; no workspace-wide ceiling "
                "was inferred. The requested priority is left unchanged for the platform to judge."
            ),
            from_caller=priority_from_caller,
        )

    ceiling = declared_priority_ceiling(evidence.levels) if evidence.levels else None
    if ceiling is None:
        return PriorityPlan(
            priority=priority,
            candidates=_priority_fallbacks(
                None,
                submitted=priority,
                workspace_id=workspace_id,
                group_id=group_id,
                quota_id=quota_id,
            ),
            from_caller=priority_from_caller,
        )

    submitted = min(int(priority), ceiling.value)
    if submitted == priority:
        return PriorityPlan(priority=priority, from_caller=priority_from_caller, ceiling=ceiling)
    return PriorityPlan(
        priority=submitted,
        warning=_priority_cap_warning(
            workspace_id=workspace_id,
            group_id=group_id,
            quota_id=quota_id,
            requested=priority,
            submitted=submitted,
            ceiling=ceiling,
        ),
        candidates=_priority_fallbacks(
            ceiling,
            submitted=submitted,
            workspace_id=workspace_id,
            group_id=group_id,
            quota_id=quota_id,
        ),
        from_caller=False,
        ceiling=ceiling,
    )


def resolve_training_priority_for_workspace(
    *,
    workspace_id: str,
    priority: int,
    session: WebSession,
    priority_from_caller: bool = False,
    group_id: str = "",
    quota_id: str = "",
    schedule: dict[str, Any] | None = None,
    gpu_count: int | None = None,
    cpu_count: int | None = None,
    memory_size_gib: int | None = None,
) -> PriorityPlan:
    """Compatibility wrapper that refuses to infer a ceiling without an offer scope."""
    if not group_id or not quota_id:
        return PriorityPlan(priority=priority, from_caller=priority_from_caller)
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
    is_fair = bool(selected.get("is_fair_workspace"))
    if not is_fair:
        return PriorityPlan(priority=priority, from_caller=priority_from_caller)
    if schedule is None:
        try:
            schedule = browser_api_module.get_notebook_schedule(
                workspace_id=workspace_id,
                session=session,
            )
        except Exception as exc:  # noqa: BLE001 - optional evidence stays unknown
            logger.debug("No workspace schedule for %s: %s", workspace_id, exc)
            schedule = {}
    return priority_plan_for_offer(
        workspace_id=workspace_id,
        group_id=group_id,
        quota_id=quota_id,
        priority=priority,
        priority_from_caller=priority_from_caller,
        is_fair=bool(selected and selected.get("is_fair_workspace")),
        schedule=schedule,
        gpu_count=gpu_count,
        cpu_count=cpu_count,
        memory_size_gib=memory_size_gib,
    )


def format_gpu_availability_summary(
    availability: list,
    gpu_type: str,
) -> str | None:
    """Format a one-line GPU availability summary for the given type.

    Returns None if no matching groups found.
    """
    same_type = [a for a in availability if gpu_type.upper() in a.gpu_type.upper()]
    if not same_type:
        return None
    parts = []
    for a in sorted(same_type, key=lambda x: x.available_gpus, reverse=True):
        part = f"{a.group_name}: {a.available_gpus} free"
        if a.low_priority_gpus > 0:
            part += f" (+{a.low_priority_gpus} preemptible)"
        parts.append(part)
    return f"  {gpu_type} availability: {' | '.join(parts)}"


def _get_default_job_image(config: Config) -> str:
    if config.docker_registry:
        return f"{config.docker_registry.rstrip('/')}/{DEFAULT_JOB_IMAGE_PATH}"
    return DEFAULT_JOB_IMAGE


def _is_usable_job_image(image: Any) -> bool:
    status = str(getattr(image, "status", "") or "").upper()
    return status in ("", "READY", "SUCCESS")


def _job_image_payload_ref(image: Any) -> str:
    return str(
        getattr(image, "url", "") or getattr(image, "name", "") or getattr(image, "image_id", "")
    ).strip()


def _find_visible_job_image(
    *,
    session: WebSession,
    workspace_id: str,
    requested: str | None,
) -> Any | None:
    sources = ("official", "public", "private", "personal-visible")
    requested_norm = str(requested or "").strip().lower()

    def images_for_source(source: str) -> list[Any]:
        try:
            images = browser_api_module.list_images_by_source(
                source=source,
                workspace_id=workspace_id,
                session=session,
            )
        except Exception:
            return []
        return [image for image in images if _is_usable_job_image(image)]

    if requested_norm:
        for source in sources:
            for image in images_for_source(source):
                fields = (
                    getattr(image, "image_id", ""),
                    getattr(image, "url", ""),
                    getattr(image, "name", ""),
                )
                if any(str(field or "").strip().lower() == requested_norm for field in fields):
                    return image
        return None

    for source in sources:
        usable = images_for_source(source)
        preferred = [
            image
            for image in usable
            if "pytorch" in _job_image_payload_ref(image).lower()
            or "pytorch" in str(getattr(image, "name", "") or "").lower()
        ]
        if preferred:
            return preferred[0]
        if usable:
            return usable[0]
    return None


def _image_type_from_visible_image(image: Any) -> str:
    source = str(getattr(image, "source", "") or "").upper()
    if source in ("SOURCE_OFFICIAL", "SOURCE_PUBLIC", "SOURCE_PRIVATE"):
        return source
    return DEFAULT_JOB_IMAGE_TYPE


def resolve_job_image(
    *,
    config: Config,
    workspace_id: str,
    image: str | None,
    session: WebSession,
) -> ResolvedJobImage:
    """Resolve job image reference and source for the browser create-job API."""
    requested = str(image or "").strip() or None
    visible_image = _find_visible_job_image(
        session=session,
        workspace_id=workspace_id,
        requested=requested,
    )
    if visible_image is not None:
        payload_ref = _job_image_payload_ref(visible_image)
        if payload_ref:
            return ResolvedJobImage(
                image=payload_ref,
                image_type=_image_type_from_visible_image(visible_image),
            )

    if requested:
        return ResolvedJobImage(image=requested, image_type=DEFAULT_JOB_IMAGE_TYPE)

    return ResolvedJobImage(
        image=_get_default_job_image(config),
        image_type=DEFAULT_JOB_IMAGE_TYPE,
    )


def wrap_in_bash(command: str) -> str:
    """Wrap a command in bash -c unless already wrapped."""
    stripped = command.strip()

    if stripped.startswith(("bash -c ", "sh -c ", "/bin/bash -c ", "/bin/sh -c ")):
        return command

    escaped = command.replace("'", "'\\''")
    return f"bash -c '{escaped}'"


def build_remote_command(config: Config, *, command: str) -> str:
    """Build the remote command with runtime exports, activation and an optional cd."""
    runtime = resolve_effective_remote_runtime(
        config,
        require_cwd=False,
        session_state=load_runtime_session_state_for_config(config),
    )
    command_parts = build_runtime_export_commands(runtime)
    activate_command = build_runtime_activate_command(runtime)
    if activate_command:
        command_parts.append(activate_command)
    cd_command = build_runtime_cd_command(runtime)
    if cd_command:
        command_parts.append(cd_command)
    command_parts.append(command)
    return " && ".join(command_parts)


def select_project_for_workspace(
    config: Config,
    *,
    workspace_id: str,
    requested: str | None,
) -> tuple[ProjectInfo, str | None]:
    """Select a project for the given workspace, with quota-aware fallback."""
    try:
        session = web_session_module.get_web_session(account=config.account_alias)
    except ValueError as e:
        raise ConfigError(str(e)) from e

    projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    if not projects:
        raise ConfigError("No projects available")

    congested = browser_api_module.check_scheduling_health(
        workspace_id=workspace_id,
        project_ids={p.project_id for p in projects},
        session=session,
    )

    requested_value = requested
    if not requested_value and not config.project_order:
        requested_value = config.job_project_id
    requested_value = resolve_project_request(
        config,
        requested_value,
        workspace_id=workspace_id,
        projects=projects,
        session=session,
    )

    shared_groups = getattr(config, "project_shared_path_groups", None)
    if not isinstance(shared_groups, dict) or not shared_groups:
        shared_groups = None

    return browser_api_module.select_project(
        projects,
        requested_value,
        shared_path_group_by_id=shared_groups,
        project_order=config.project_order or None,
        congested_projects=congested or None,
    )


def _session_for_config(config: Config) -> WebSession:
    """Open the browser session for the account selected by this config."""

    return web_session_module.get_web_session(account=config.account_alias)


def max_running_time_ms(max_time_hours: float) -> str:
    """Convert the operator's hours into the platform's milliseconds.

    Named so it can be tested: surface ablation found that `--max-time`, the one job-creation knob that sets
    a ceiling the platform enforces, had no test naming it, and the conversion was an inline expression
    nobody could assert on. The platform field is a string of integer milliseconds, and truncation is
    deliberate - sub-millisecond fractions of an hour are not a thing an operator means.
    """
    if max_time_hours <= 0:
        raise ValueError("max time must be a positive number of hours")
    return str(int(max_time_hours * 3600 * 1000))


def resolve_priority_plan_for_config(
    *,
    config: Config,
    workspace_id: str,
    priority: int,
    priority_from_caller: bool,
    session: Optional[WebSession] = None,
) -> PriorityPlan:
    """Legacy unscoped wrapper; current commands plan priority after selecting an offer.

    Without a group and quota this leaves the requested value unchanged. It must not be used
    to infer a workspace-wide ceiling before resource selection.
    """
    if session is None:
        session = _session_for_config(config)
    return resolve_training_priority_for_workspace(
        workspace_id=workspace_id,
        priority=priority,
        session=session,
        priority_from_caller=priority_from_caller,
    )


def prepare_training_job_launch(
    *,
    config: Config,
    name: str,
    command: str,
    resource: str,
    framework: str,
    location: Optional[str],
    project_id: str,
    workspace_id: str,
    image: Optional[str],
    priority: int,
    nodes: int,
    max_time_hours: float,
    priority_from_caller: bool = True,
    priority_plan: PriorityPlan | None = None,
    project_name: Optional[str] = None,
    auto_fault_tolerance: bool = False,
    fault_tolerance_max_retry: int | None = None,
    keep_alive_on_success: int | None = None,
    keep_alive_on_fail: int | None = None,
    exact_quota: str | None = None,
    exact_group: bool = False,
    auto_select_offer: bool = False,
    selected_offer: object | None = None,
    dataset_info: Optional[list[dict[str, str]]] = None,
    shm_size: int | None = None,
    session: Optional[WebSession] = None,
) -> PreparedJobLaunch:
    if nodes <= 0:
        raise ValueError("nodes must be at least 1")
    if session is None:
        session = _session_for_config(config)

    wrapped_command = wrap_in_bash(command)
    final_command = build_remote_command(config, command=wrapped_command)
    max_time_ms = max_running_time_ms(max_time_hours)

    if selected_offer is not None:
        selected_resource = training_resource_selection_from_offer(selected_offer)
    else:
        selected_resource = resolve_training_resource_selection(
            config=config,
            workspace_id=workspace_id,
            resource=resource,
            prefer_location=location,
            exact_quota=exact_quota,
            exact_group=exact_group,
            auto_select_offer=auto_select_offer,
            priority=priority_plan.priority if priority_plan is not None else None,
            requested_priority=priority,
            session=session,
        )

    if priority_plan is None:
        priority_plan = resolve_training_priority_for_workspace(
            workspace_id=workspace_id,
            priority=priority,
            session=session,
            priority_from_caller=priority_from_caller,
            group_id=selected_resource.logic_compute_group_id,
            quota_id=selected_resource.quota_id,
            gpu_count=selected_resource.gpu_count,
            cpu_count=selected_resource.cpu_count,
            memory_size_gib=selected_resource.memory_size_gib,
        )
    effective_priority = priority_plan.priority
    priority_warning = priority_plan.warning

    # Always resolve a positive shm value. Omitting the field makes the platform choose its own
    # default (observed as 0 on this console), which is indistinguishable from "no /dev/shm" and
    # drifts by image; the OpenAPI create path already sends 200 by default.  An explicit value is
    # never capped - the user may deliberately ask for more than the row's nominal memory - but the
    # CLI default is capped so a small CPU row cannot receive a nonsensical /dev/shm.
    job_shm_size = getattr(config, "job_shm_size", None)
    effective_shm_size: int | None
    if shm_size is not None:
        effective_shm_size = shm_size
    elif job_shm_size is not None:
        effective_shm_size = job_shm_size
    else:
        effective_shm_size = config.shm_size
    if effective_shm_size is None:
        memory_size = int(selected_resource.memory_size_gib or 0)
        effective_shm_size = (
            min(DEFAULT_JOB_SHM_SIZE, memory_size) if memory_size > 0 else DEFAULT_JOB_SHM_SIZE
        )
    resolved_shm_size = int(effective_shm_size)
    if resolved_shm_size < 1:
        raise ValueError(
            "Shared memory size must be >= 1 (set --shm, INSPIRE_JOB_SHM_SIZE, "
            "[job].shm_size, or [defaults].shm_size)."
        )

    resolved_image = resolve_job_image(
        config=config,
        workspace_id=workspace_id,
        image=image,
        session=session,
    )
    framework_item: dict[str, Any] = {
        "image_type": resolved_image.image_type,
        "image": resolved_image.image,
        "instance_count": nodes,
        "resource_spec_price": selected_resource.resource_spec_price,
        "cpu": selected_resource.cpu_count,
        "gpu_count": selected_resource.gpu_count,
        "mem_gi": selected_resource.memory_size_gib,
    }
    framework_item["shm_gi"] = resolved_shm_size

    payload: dict[str, Any] = {
        "name": name,
        "command": final_command,
        "framework": framework,
        "logic_compute_group_id": selected_resource.logic_compute_group_id,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "task_priority": effective_priority,
        "max_running_time_ms": max_time_ms,
        "framework_config": [framework_item],
    }
    if auto_fault_tolerance:
        payload["auto_fault_tolerance"] = True
        max_retry = 100 if fault_tolerance_max_retry is None else int(fault_tolerance_max_retry)
        if not 1 <= max_retry <= 2000:
            raise ValueError("Fault-tolerance max retry must be between 1 and 2000.")
        payload["fault_tolerance_max_retry"] = max_retry
    if keep_alive_on_success is not None:
        payload["reserve_on_success_ms"] = str(keep_alive_on_success * 1000)
    if keep_alive_on_fail is not None:
        payload["reserve_on_fail_ms"] = str(keep_alive_on_fail * 1000)
    if dataset_info is not None:
        # Each entry carries the backend path ValidateDataset resolved for it, which is what makes
        # the mount real; like the notebook create body, the field is absent rather than empty.
        payload["dataset_info"] = dataset_info

    return PreparedJobLaunch(
        payload=payload,
        wrapped_command=wrapped_command,
        max_time_ms=max_time_ms,
        resource=selected_resource,
        image=resolved_image,
        warnings=(priority_warning,) if priority_warning else (),
        priority_candidates=priority_plan.candidates,
        priority_requested=priority,
        priority_from_caller=priority_plan.from_caller,
        priority_ceiling=priority_plan.ceiling,
    )


class PriorityRefusedError(Exception):
    """The platform refused the ``task_priority`` this submit asked it to take.

    Raised rather than quietly submitting another number, and rather than passing the bare
    ``InvalidParameter`` text through: the message names what was asked for and what was
    tried, and ``hint`` names an alternative this CLI has watched the platform accept when
    there is one to name.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.hint = hint
        super().__init__(message)


def _is_priority_range_refusal(exc: BaseException) -> bool:
    """Whether the platform refused the submitted ``task_priority`` specifically.

    Matched on the stable ``InvalidParameter`` code *plus* priority wording, never on the
    Chinese message alone: the code is what the API exposes, and requiring both keeps a
    different ``InvalidParameter`` (an unknown field, a bad image ref) from being read as a
    statement about priority.
    """
    text = str(exc)
    return bool(_PRIORITY_REFUSAL_CODE.search(text)) and bool(
        _PRIORITY_REFUSAL_WORDING.search(text)
    )


def _fallback_priority_is_safe(prepared: PreparedJobLaunch, value: int) -> bool:
    """Whether retrying this launch at ``value`` keeps its fault-tolerance posture.

    Fault tolerance is decided once, from the priority the launch was prepared to submit. A
    fallback below :data:`PREEMPT_MIN_PRIORITY` makes the job preemptible, so it is only safe when
    the payload already carries the platform's restart loop (``auto_fault_tolerance``) or the
    launch was preemptible to begin with. Otherwise the fallback would silently turn a
    restart-protected decision into an unprotected preemptible job.
    """
    submitted = prepared.payload.get("task_priority")
    if not isinstance(submitted, int) or not isinstance(value, int):
        return True
    if submitted < PREEMPT_MIN_PRIORITY or value >= PREEMPT_MIN_PRIORITY:
        return True
    return bool(prepared.payload.get("auto_fault_tolerance"))


def _withheld_priority_candidates(prepared: PreparedJobLaunch) -> tuple[int, ...]:
    """The offer's lower priorities this launch must not fall back to."""
    return tuple(
        value
        for value in prepared.priority_candidates
        if not _fallback_priority_is_safe(prepared, value)
    )


def _priority_attempt_order(prepared: PreparedJobLaunch) -> tuple[Any, ...]:
    """The priorities to submit for this launch, the capped one first.

    A caller's own uncapped priority stands alone: its refusal is reported, never answered with
    a lower historical value. Only a negotiated priority may be followed by this offer's fallback
    evidence, bounded by :data:`MAX_PRIORITY_ATTEMPTS`. A candidate below
    :data:`PREEMPT_MIN_PRIORITY` is withheld when the prepared launch has no restart loop, because
    submitting it would change the job's safety without asking.
    """
    if prepared.priority_from_caller:
        return (prepared.payload.get("task_priority"),)
    ordered: list[Any] = [prepared.payload.get("task_priority")]
    for value in prepared.priority_candidates:
        if value in ordered or not _fallback_priority_is_safe(prepared, value):
            continue
        ordered.append(value)
    return tuple(ordered[:MAX_PRIORITY_ATTEMPTS])


def _asked_for_priority(prepared: PreparedJobLaunch) -> Any:
    if prepared.priority_requested is not None:
        return prepared.priority_requested
    return prepared.payload.get("task_priority")


def _priority_refused_error(
    *,
    prepared: PreparedJobLaunch,
    tried: tuple[int, ...],
    platform_error: Exception,
) -> PriorityRefusedError:
    """Turn a platform refusal into words that name the values actually involved.

    Three things and no more belong here: which priority the platform refused, whether the CLI
    had capped it and to what, and why nothing else was tried. The ceiling is named with the
    declaration or the observation it came from, so a refusal is checkable against
    ``inspire job offers`` rather than against a number the CLI invented.
    """
    workspace_id = str(prepared.payload.get("workspace_id") or "")
    ceiling = prepared.priority_ceiling
    asked_for = _asked_for_priority(prepared)
    submitted = tried[0] if tried else None

    if len(tried) > 1:
        values = ", ".join(str(value) for value in tried)
        refused = f"every priority this CLI submitted ({values})"
    elif prepared.priority_from_caller:
        refused = f"priority {asked_for}, the value you asked for"
    elif submitted != asked_for:
        refused = f"priority {submitted}, which this CLI capped your priority {asked_for} to"
    else:
        refused = f"priority {submitted}"

    lower = (
        _priority_fallbacks(
            ceiling,
            submitted=min(tried),
            workspace_id=workspace_id,
            group_id=prepared.resource.logic_compute_group_id,
            quota_id=prepared.resource.quota_id,
        )
        if tried
        else ()
    )
    held_back = () if prepared.priority_from_caller else _withheld_priority_candidates(prepared)
    if ceiling is None:
        ceiling_fact = (
            "No priority ceiling could be derived for the selected Scheduling Offer; the "
            "number went out exactly as asked for."
        )
    else:
        ceiling_fact = (
            f"The selected offer's ceiling is {ceiling.value}: {ceiling.source_phrase()}."
        )
    if prepared.priority_from_caller:
        why = "The CLI does not replace a priority you passed, so nothing else was submitted."
    elif len(tried) > 1:
        why = f"It steps down once, to {tried[-1]}, and does not guess past one step on its own."
    elif held_back:
        values = ", ".join(str(value) for value in held_back)
        why = (
            f"Every lower priority this offer has evidence for ({values}) is below "
            f"{PREEMPT_MIN_PRIORITY}, which would make the job preemptible without the restart "
            "loop this launch did not enable, so nothing else was submitted."
        )
    else:
        why = "No lower value has evidence for this offer, so nothing else was tried."

    if held_back:
        hint = (
            f"Pass --fault-tolerant to allow a fallback such as {held_back[0]}, or pass "
            f"--priority {held_back[0]}, which enables the low-priority restart loop for a "
            "preemptible job."
        )
    elif lower:
        hint = (
            f"Pass --priority {lower[0]}, the highest lower value this offer has evidence "
            "for, or pick another value yourself."
        )
    else:
        hint = (
            "Pass --priority with a value this workspace accepts: `inspire job offers` lists the "
            "priority levels each Scheduling Offer declares."
        )
    return PriorityRefusedError(
        f"In workspace {workspace_id} the platform refused {refused}. {ceiling_fact} {why} "
        f"No job was created. Last platform answer: {platform_error}",
        hint=hint,
    )


def _create_training_job_once(
    *,
    payload: dict[str, Any],
    prepared: PreparedJobLaunch,
    name: str,
    submitted_at: datetime,
    session: WebSession,
) -> tuple[Any, bool]:
    """One create attempt, reconciled by launch identity if its response was ambiguous."""
    try:
        return browser_api_module.create_training_job(payload, session=session), False
    except Exception as exc:
        reconciled_job = None
        if _is_ambiguous_create_error(exc):
            reconciled_job = _find_reconciled_training_job(
                prepared=prepared,
                name=name,
                submitted_at=submitted_at,
                session=session,
            )
        if reconciled_job is None:
            raise
        logger.warning(
            "Training-job create response was ambiguous; reconciled job %s by exact launch identity",
            reconciled_job.job_id,
        )
        return (
            {
                "code": 0,
                "data": {
                    "job_id": reconciled_job.job_id,
                    "status": reconciled_job.status,
                    "reconciled": True,
                },
            },
            True,
        )


def submit_prepared_training_job(
    *,
    config: Config,
    prepared: PreparedJobLaunch,
    name: str,
    session: Optional[WebSession] = None,
) -> JobSubmission:
    """Perform the remote mutation for a prepared launch.

    Task priority is already capped to the selected Scheduling Offer's ceiling by
    :func:`priority_plan_for_offer`, so the normal path is exactly one create
    with the number the CLI said it would submit. A retry belongs to the leftover case where no
    ceiling could be derived from a declaration and the platform refuses anyway: the next value
    this offer has evidence for is tried, each step is reported, and the number of creates is
    bounded by :data:`MAX_PRIORITY_ATTEMPTS`. A fallback below :data:`PREEMPT_MIN_PRIORITY` is
    withheld when the prepared payload has no ``auto_fault_tolerance`` restart loop: fault
    tolerance was decided from the non-preemptible priority this launch planned, and a refusal is
    reported instead of quietly creating a preemptible job without that protection. A priority the
    caller passed at or below the ceiling is never changed: a refusal of it raises
    :class:`PriorityRefusedError` with the platform's own words and a hint, and nothing is
    submitted behind their back.

    A refusal is retried only when it is about the priority - ``InvalidParameter`` plus priority
    wording - and a retry changes only ``task_priority``. The Scheduling Offer is not re-resolved
    per attempt, because that could move the job to a different compute group than the one the
    caller previewed; where the platform declares per-offer priority levels, resource resolution
    has already matched the offer against them.
    """
    if session is None:
        session = _session_for_config(config)

    attempts = _priority_attempt_order(prepared)
    refused: list[int] = []
    result: Any = None
    reconciled = False
    accepted_priority: int | None = None
    priority_note: str | None = None

    for index, value in enumerate(attempts):
        payload = prepared.payload if index == 0 else {**prepared.payload, "task_priority": value}
        try:
            result, reconciled = _create_training_job_once(
                payload=payload,
                prepared=prepared,
                name=name,
                submitted_at=datetime.now(timezone.utc),
                session=session,
            )
        except Exception as exc:
            if value is None or not _is_priority_range_refusal(exc):
                raise
            if index + 1 < len(attempts):
                refused.append(int(value))
                logger.warning(
                    "Platform refused priority %s in workspace %s; trying priority %s",
                    value,
                    payload.get("workspace_id"),
                    attempts[index + 1],
                )
                continue
            raise _priority_refused_error(
                prepared=prepared,
                tried=(*refused, int(value)),
                platform_error=exc,
            ) from exc
        accepted_priority = int(value) if value is not None else None
        if value is not None:
            record_accepted_priority(
                str(payload.get("workspace_id") or ""),
                int(value),
                group_id=prepared.resource.logic_compute_group_id,
                quota_id=prepared.resource.quota_id,
            )
        if refused:
            ceiling = prepared.priority_ceiling
            reason = (
                f"the next value at or below its ceiling of {ceiling.value} that this CLI has "
                "evidence for"
                if ceiling is not None
                else "the next lower value this CLI has evidence for"
            )
            priority_note = (
                f"The platform refused priority {', '.join(str(item) for item in refused)}, so the "
                f"job was created at priority {value} ({_priority_label(int(value))}) rather than "
                f"the requested priority {_asked_for_priority(prepared)} — {reason}."
            )
        break

    data = result.get("data", {}) if isinstance(result, dict) else {}
    job_id = data.get("job_id")

    return JobSubmission(
        job_id=job_id,
        data=data,
        result=result,
        wrapped_command=prepared.wrapped_command,
        max_time_ms=prepared.max_time_ms,
        reconciled=reconciled,
        priority=accepted_priority,
        priority_note=priority_note,
    )


def _is_ambiguous_create_error(exc: Exception) -> bool:
    """Return whether a create failure could have happened after acceptance."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    return re.search(r"(?:API returned|API error) 5\d\d\b", str(exc)) is not None


def _platform_created_at(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    if numeric > 100_000_000_000:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _find_reconciled_training_job(
    *,
    prepared: PreparedJobLaunch,
    name: str,
    submitted_at: datetime,
    session: WebSession,
) -> Any | None:
    """Find one newly accepted job after an ambiguous create response."""
    try:
        jobs, _total = browser_api_module.list_jobs(
            workspace_id=str(prepared.payload.get("workspace_id") or "") or None,
            page_num=1,
            page_size=100,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unable to reconcile ambiguous training-job create: %s", exc)
        return None

    expected_command = str(prepared.payload.get("command") or "")
    expected_project = str(prepared.payload.get("project_id") or "")
    earliest = submitted_at.timestamp() - 300
    matches = []
    for job in jobs:
        created_at = _platform_created_at(getattr(job, "created_at", ""))
        if created_at is None or created_at.timestamp() < earliest:
            continue
        if getattr(job, "name", "") != name:
            continue
        if str(getattr(job, "command", "") or "") != expected_command:
            continue
        if expected_project and str(getattr(job, "project_id", "") or "") != expected_project:
            continue
        matches.append(job)

    return matches[0] if len(matches) == 1 else None


class DatasetMountNotAppliedError(Exception):
    """The platform accepted a dataset-bearing job and then attached none of it.

    Raised when a created job comes back from ``GetJob`` with a ``dataset_info`` that does not cover
    the versions it was submitted with. The create accepts the field and answers 200 either way, so
    without this read-back a job runs to completion against missing data - the silent drop issue #80
    records for the OpenAPI ``CreateJob`` surface.
    """

    def __init__(self, job_id: str, missing: list[str], submitted: int) -> None:
        self.job_id = job_id
        self.missing = missing
        self.submitted = submitted
        super().__init__(
            f"Job {job_id} was created with {submitted} dataset version(s), but the platform "
            f"reports these as not attached: {', '.join(missing)}. The platform silently dropped "
            "the dataset_info it accepted, so the job is running without its data. Stop it before "
            "it writes results against missing input."
        )


def _dataset_pair_of(entry: Any) -> tuple[str, str] | None:
    """The ``(dataset, version)`` a ``dataset_info`` entry names, from either spelling."""
    if not isinstance(entry, dict):
        return None
    dataset_id = entry.get("dataset_id") or entry.get("dataset_code") or entry.get("name")
    version_id = entry.get("version_id") or entry.get("version_code") or entry.get("version")
    if not dataset_id or not version_id:
        return None
    return validation_pair_key(dataset_id, version_id)


def verify_mounted_datasets(
    *,
    job_id: str,
    dataset_info: list[dict[str, str]],
    session: Optional[WebSession] = None,
) -> list[dict[str, Any]]:
    """Read the created job back and refuse it unless every submitted version is attached.

    Returns the ``dataset_info`` the platform reports. A job whose mount list does not cover what
    was submitted raises: nothing else about the job will reveal that its data never arrived.
    """
    if session is None:
        session = web_session_module.get_web_session()
    detail = browser_api_module.get_job_detail(job_id, session=session)
    job = detail.get("data") if isinstance(detail, dict) else None
    mounted = job.get("dataset_info") if isinstance(job, dict) else None
    mounted = mounted if isinstance(mounted, list) else []

    mounted_pairs = {_dataset_pair_of(entry) for entry in mounted}
    mounted_pairs.discard(None)
    missing = [
        f"{entry.get('dataset_id')}@{entry.get('version_id')}"
        for entry in dataset_info
        if _dataset_pair_of(entry) not in mounted_pairs
    ]
    if missing:
        raise DatasetMountNotAppliedError(job_id, missing, len(dataset_info))
    return [entry for entry in mounted if isinstance(entry, dict)]


#: The canonical statuses in which a job still holds its name. Anything else has ended, so a
#: create reusing that name is a new run rather than a collision.
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("PENDING", "RUNNING", "QUEUING")

#: One ListJobs page of the duplicate-name guard. The platform documents no ceiling this CLI can
#: rely on, so the guard pages at the size the rest of job submission already reads.
_ACTIVE_JOB_LOOKUP_PAGE_SIZE = 100

#: How many pages of one active status the guard reads before it gives up. Active jobs are a small
#: set in a live workspace and the newest pages hold them, but a name can sit anywhere in a status.
#: The cap keeps every create's lookup bounded even if the platform's total never runs out.
_ACTIVE_JOB_LOOKUP_MAX_PAGES = 10


class ActiveJobLookupIncomplete(RuntimeError):
    """The duplicate-name guard could not read every row the platform said exists."""


def find_active_job_by_name(
    *,
    name: str,
    workspace_id: str,
    session: Optional[WebSession] = None,
) -> JobInfo | None:
    """Return the live job already carrying this name in this workspace, if there is one.

    The status filter is pushed to the platform (`list_jobs_with_canonical_status`) instead of
    paging the whole workspace and filtering here, and the three active statuses are asked for
    separately because the server filters on one status group per call. A status is paged until
    the name is found or the server's total says the status is exhausted, capped at
    :data:`_ACTIVE_JOB_LOOKUP_MAX_PAGES` pages so the guard cannot walk an unbounded list.
    """
    for status in ACTIVE_JOB_STATUSES:
        fetched = 0
        completed = False
        for page_num in range(1, _ACTIVE_JOB_LOOKUP_MAX_PAGES + 1):
            jobs, total = browser_api_module.list_jobs_with_canonical_status(
                status=status,
                workspace_id=workspace_id,
                page_num=page_num,
                page_size=_ACTIVE_JOB_LOOKUP_PAGE_SIZE,
                session=session,
            )
            fetched += len(jobs)
            for job in jobs:
                if getattr(job, "name", None) == name:
                    return job
            if not jobs or fetched >= int(total or 0):
                completed = True
                break
        if not completed:
            raise ActiveJobLookupIncomplete(
                f"active {status.lower()} job-name lookup stopped after "
                f"{_ACTIVE_JOB_LOOKUP_MAX_PAGES} pages ({fetched} rows fetched, "
                f"{int(total or 0)} reported)"
            )
    return None


def submit_training_job(
    **kwargs: Any,
) -> JobSubmission:
    """Compatibility wrapper that prepares and immediately submits a job."""
    config = kwargs["config"]
    session = kwargs.get("session") or _session_for_config(config)
    prepared = prepare_training_job_launch(**{**kwargs, "session": session})
    return submit_prepared_training_job(
        config=config,
        prepared=prepared,
        name=kwargs["name"],
        session=session,
    )


__all__ = [
    "DatasetMountNotAppliedError",
    "MAX_PRIORITY_ATTEMPTS",
    "JobSubmission",
    "PreparedJobLaunch",
    "PriorityCeiling",
    "PriorityPlan",
    "PriorityRefusedError",
    "ResolvedJobImage",
    "TrainingResourceSelection",
    "build_remote_command",
    "declared_priority_ceiling",
    "find_active_job_by_name",
    "format_gpu_availability_summary",
    "prepare_training_job_launch",
    "resolve_fault_tolerance",
    "resolve_job_image",
    "resolve_priority_plan_for_config",
    "resolve_training_priority_for_workspace",
    "resolve_training_resource_selection",
    "select_project_for_workspace",
    "submit_training_job",
    "submit_prepared_training_job",
    "verify_mounted_datasets",
    "wrap_in_bash",
]
