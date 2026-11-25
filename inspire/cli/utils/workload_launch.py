"""Exact preparation and submission for HPC and Ray workload launches."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import click

from inspire.cli.utils.launch_preview import LaunchPreview
from inspire.cli.utils.training_resources import parse_quota_spec
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_profile
from inspire.config.workspaces import select_workspace_id
from inspire.config.projects import resolve_project_request
from inspire.platform.web import browser_api
from inspire.platform.web.browser_api.images import (
    IMAGE_READY_STATUSES,
    CustomImageInfo,
)
from inspire.platform.web.browser_api.projects import ProjectInfo
from inspire.platform.web.browser_api.workload_contracts import (
    WorkloadOffer,
    WorkloadOfferDiscovery,
)
from inspire.platform.web.session import WebSession, get_web_session

_IMAGE_SOURCES = ("official", "public", "private", "personal-visible")
_LAUNCHABLE_IMAGE_STATUSES = IMAGE_READY_STATUSES  # defined by the API layer, not here


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return deepcopy(value)


@dataclass(frozen=True)
class PreparedWorkloadLaunch:
    """Invocation-local immutable launch containing the exact platform request."""

    kind: str
    preview: LaunchPreview
    platform_payload: Mapping[str, Any]
    session: WebSession = field(repr=False, compare=False)

    @classmethod
    def build(
        cls,
        *,
        kind: str,
        preview: LaunchPreview,
        platform_payload: dict[str, Any],
        session: WebSession,
    ) -> "PreparedWorkloadLaunch":
        return cls(
            kind=kind,
            preview=preview,
            platform_payload=_freeze(platform_payload),
            session=session,
        )

    def request_dict(self) -> dict[str, Any]:
        """Return a mutable copy suitable for the transport adapter."""
        return _thaw(self.platform_payload)


def _required_text(intent: Mapping[str, Any], key: str, *, kind: str) -> str:
    value = str(intent.get(key) or "").strip()
    if not value:
        raise ConfigError(
            f"{kind} launch requires {key!r} from an explicit option or selected profile"
        )
    return value


def _positive_int(value: Any, *, field_name: str, default: int | None = None) -> int:
    if value in (None, ""):
        if default is None:
            raise ConfigError(f"{field_name} is required")
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be an integer") from exc
    if number < 1:
        raise ConfigError(f"{field_name} must be positive")
    return number


def _integer(value: Any, *, field_name: str, default: int | None = None) -> int:
    if value in (None, ""):
        if default is None:
            raise ConfigError(f"{field_name} is required")
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be an integer") from exc


def _boolean(value: Any, *, field_name: str, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{field_name} must be a boolean")


def _resolve_workspace(
    config: Config,
    intent: Mapping[str, Any],
    *,
    explicit_workspace_id: str | None,
    session: WebSession | None = None,
) -> tuple[str, str]:
    workspace = str(intent.get("workspace") or "").strip()
    if not workspace and not explicit_workspace_id:
        raise ConfigError(
            "Workload launch requires an exact workspace alias or --workspace-id; "
            "workspace discovery from other objects is not allowed"
        )
    workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=workspace or None,
        explicit_workspace_id=explicit_workspace_id,
        session=session,
    )
    if not workspace_id:
        raise ConfigError("Could not resolve the requested workspace")
    return workspace_id, workspace or workspace_id


def _resolve_project(
    config: Config,
    requested: str,
    *,
    workspace_id: str,
    session: WebSession,
) -> ProjectInfo:
    projects = browser_api.list_projects(workspace_id=workspace_id, session=session)
    if not projects:
        raise ConfigError(f"No projects are visible in workspace {workspace_id}")

    target = resolve_project_request(
        config,
        requested,
        workspace_id=workspace_id,
        projects=projects,
        session=session,
    )
    by_id = [project for project in projects if project.project_id == target]
    if len(by_id) == 1:
        return by_id[0]
    by_name = [project for project in projects if project.name == requested]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        ids = ", ".join(sorted(project.project_id for project in by_name))
        raise ConfigError(f"Project name {requested!r} is ambiguous in this workspace: {ids}")
    raise ConfigError(f"Project {requested!r} is not visible in workspace {workspace_id}")


def _offer_description(offer: WorkloadOffer) -> str:
    return (
        f"{offer.logic_compute_group_name} ({offer.logic_compute_group_id}), "
        f"quota={offer.quota_id}, GPU={offer.gpu_count}, CPU={offer.cpu_count}, "
        f"MEM={offer.memory_size_gib}"
    )


def _resolve_offer(
    discovery: WorkloadOfferDiscovery,
    *,
    group: str,
    quota: str,
) -> WorkloadOffer:
    try:
        quota_gpu, quota_cpu, quota_memory = parse_quota_spec(quota)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    group_matches = [
        offer
        for offer in discovery.offers
        if group in {offer.logic_compute_group_id, offer.logic_compute_group_name}
    ]
    if not group_matches:
        raise ConfigError(
            f"Compute group {group!r} has no live {discovery.workload} Scheduling Offers"
        )
    group_ids = {offer.logic_compute_group_id for offer in group_matches}
    if len(group_ids) > 1:
        raise ConfigError(
            f"Compute group name {group!r} is ambiguous: {', '.join(sorted(group_ids))}"
        )
    matches = [
        offer
        for offer in group_matches
        if (offer.gpu_count, offer.cpu_count, offer.memory_size_gib)
        == (quota_gpu, quota_cpu, quota_memory)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        available = "; ".join(_offer_description(offer) for offer in group_matches)
        raise ConfigError(
            f"Exact quota {quota!r} is not a live Scheduling Offer for group {group!r}. "
            f"Available: {available or '<none>'}"
        )
    details = "; ".join(_offer_description(offer) for offer in matches)
    raise ConfigError(f"Exact Scheduling Offer is ambiguous: {details}")


def _list_images(*, workspace_id: str, session: WebSession) -> list[CustomImageInfo]:
    by_id: dict[str, CustomImageInfo] = {}
    for source in _IMAGE_SOURCES:
        for image in browser_api.list_images_by_source(
            source=source,
            workspace_id=workspace_id,
            session=session,
        ):
            if image.image_id:
                by_id.setdefault(image.image_id, image)
    return list(by_id.values())


def _image_names(image: CustomImageInfo) -> set[str]:
    names = {image.image_id, image.url, image.name}
    if image.name and image.version:
        names.add(f"{image.name}:{image.version}")
    return {name for name in names if name}


def _resolve_image(
    requested: str,
    *,
    workspace_id: str,
    session: WebSession,
) -> CustomImageInfo:
    matches = [
        image
        for image in _list_images(workspace_id=workspace_id, session=session)
        if requested in _image_names(image)
    ]
    launchable = [
        image for image in matches if str(image.status or "").upper() in _LAUNCHABLE_IMAGE_STATUSES
    ]
    if len(launchable) == 1:
        return launchable[0]
    if len(launchable) > 1:
        labels = ", ".join(
            sorted(f"{image.image_id} ({image.source}, {image.url})" for image in launchable)
        )
        raise ConfigError(f"Image {requested!r} is ambiguous: {labels}")
    if matches:
        statuses = ", ".join(
            sorted(f"{image.image_id}:{image.status or '<missing>'}" for image in matches)
        )
        allowed = ", ".join(sorted(_LAUNCHABLE_IMAGE_STATUSES))
        raise ConfigError(f"Image {requested!r} is not launchable ({allowed}): {statuses}")
    raise ConfigError(f"Image {requested!r} is not visible in workspace {workspace_id}")


def _availability_for_offer(
    discovery: WorkloadOfferDiscovery,
    offer: WorkloadOffer,
    *,
    session: WebSession,
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        rows = browser_api.get_workload_availability(discovery, session=session)
    except Exception as exc:  # noqa: BLE001 - advisory only
        return None, [f"Availability could not be fetched: {exc}"]
    selected = next(
        (row for row in rows if row.logic_compute_group_id == offer.logic_compute_group_id),
        None,
    )
    if selected is None:
        return None, ["Availability did not include the selected compute group"]
    warnings = [f"Availability signal error: {selected.error}"] if selected.error else []
    return asdict(selected), warnings


def _offer_payload(offer: WorkloadOffer) -> dict[str, Any]:
    return {
        "workspace_id": offer.workspace_id,
        "logic_compute_group_id": offer.logic_compute_group_id,
        "logic_compute_group_name": offer.logic_compute_group_name,
        "quota_id": offer.quota_id,
        "gpu_count": offer.gpu_count,
        "cpu_count": offer.cpu_count,
        "memory_size_gib": offer.memory_size_gib,
        "gpu_type": offer.gpu_type,
    }


def _cap_priority_at_project(
    intent: dict[str, Any],
    sources: dict[str, str],
    project: ProjectInfo,
) -> None:
    """Clamp the resolved priority to the project's own priority ceiling.

    The platform rejects launches whose priority exceeds the project's, so cap
    here and warn instead of failing server-side; explicit --priority and the
    config default are treated the same. A missing or non-numeric project
    priority keeps the previous pass-through behavior. Capped sources are
    suffixed with "+capped_at_project" (plain strings, so dry-run previews
    stay JSON-serializable) and the warning goes to stderr so --json stdout
    remains a single clean envelope.
    """
    requested = intent.get("priority")
    if requested in (None, ""):
        return
    try:
        project_cap = int(str(project.priority_name or "").strip())
    except ValueError:
        return
    try:
        requested_priority = int(requested)
    except (TypeError, ValueError):
        return  # the payload builders report the malformed value later
    if requested_priority <= project_cap:
        return
    intent["priority"] = project_cap
    sources["priority"] = f"{sources.get('priority', 'unknown')}+capped_at_project"
    click.echo(
        f"Warning: priority {requested_priority} capped at "
        f"project priority {project_cap} ({project.name})",
        err=True,
    )


def _base_resolution(
    *,
    kind: str,
    config: Config,
    profile_name: str | None,
    explicit: Mapping[str, Any],
    explicit_workspace_id: str | None,
    session: WebSession | None,
) -> tuple[
    dict[str, Any],
    dict[str, str],
    str,
    str,
    ProjectInfo,
    WorkloadOfferDiscovery,
    WorkloadOffer,
    CustomImageInfo,
    WebSession,
    dict[str, Any] | None,
    list[str],
]:
    intent, sources = apply_profile(
        profiles=config.profiles,
        kind=kind,
        name=profile_name,
        explicit=explicit,
    )
    if intent.get("priority") in (None, "") and config.default_priority is not None:
        intent["priority"] = config.default_priority
        sources["priority"] = "config:default_priority"
    if session is None:
        session = get_web_session(account=config.account_alias)
    workspace_id, workspace_label = _resolve_workspace(
        config,
        intent,
        explicit_workspace_id=explicit_workspace_id,
        session=session,
    )
    project = _resolve_project(
        config,
        _required_text(intent, "project", kind=kind),
        workspace_id=workspace_id,
        session=session,
    )
    _cap_priority_at_project(intent, sources, project)
    if intent.get("priority") in (None, ""):
        project_priority = str(project.priority_name or "").strip()
        try:
            intent["priority"] = int(project_priority) if project_priority else 4
        except ValueError:
            intent["priority"] = 4
        sources["priority"] = (
            "live_project.priority_name" if project_priority else "platform_default"
        )
    discovery = (
        browser_api.list_hpc_offers(workspace_id=workspace_id, session=session)
        if kind == "hpc"
        else browser_api.list_ray_offers(workspace_id=workspace_id, session=session)
    )
    offer = _resolve_offer(
        discovery,
        group=_required_text(intent, "group", kind=kind),
        quota=_required_text(intent, "quota", kind=kind),
    )
    image = _resolve_image(
        _required_text(intent, "image", kind=kind),
        workspace_id=workspace_id,
        session=session,
    )
    availability, warnings = _availability_for_offer(discovery, offer, session=session)
    return (
        intent,
        sources,
        workspace_id,
        workspace_label,
        project,
        discovery,
        offer,
        image,
        session,
        availability,
        warnings,
    )


def prepare_hpc_launch(
    *,
    config: Config,
    name: str,
    entrypoint: str,
    profile_name: str | None = None,
    explicit: Mapping[str, Any],
    explicit_workspace_id: str | None = None,
    session: WebSession | None = None,
) -> PreparedWorkloadLaunch:
    """Resolve exact live bindings and build the native HPC create payload."""
    name = str(name or "").strip()
    entrypoint = str(entrypoint or "").strip()
    if not name:
        raise ConfigError("HPC launch requires a non-empty name")
    if not entrypoint:
        raise ConfigError("HPC launch requires a non-empty entrypoint")
    if entrypoint.lstrip().startswith("#!") or "#SBATCH" in entrypoint:
        raise ConfigError("HPC entrypoint must be the Slurm body, not a full sbatch script")

    (
        intent,
        sources,
        workspace_id,
        workspace_label,
        project,
        _discovery,
        offer,
        image,
        session,
        availability,
        warnings,
    ) = _base_resolution(
        kind="hpc",
        config=config,
        profile_name=profile_name,
        explicit=explicit,
        explicit_workspace_id=explicit_workspace_id,
        session=session,
    )
    instance_count = _positive_int(
        intent.get("instance_count"), field_name="instance_count", default=1
    )
    number_of_tasks = _positive_int(
        intent.get("number_of_tasks"), field_name="number_of_tasks", default=1
    )
    cpus_per_task = _positive_int(
        intent.get("cpus_per_task"),
        field_name="cpus_per_task",
        default=max(1, offer.cpu_count),
    )
    memory_per_cpu = _positive_int(
        intent.get("memory_per_cpu"),
        field_name="memory_per_cpu",
        default=max(1, offer.memory_size_gib // cpus_per_task),
    )
    sbatch_script: dict[str, Any] = {
        "number_of_tasks": number_of_tasks,
        "cpus_per_task": cpus_per_task,
        "memory_per_cpu": f"{memory_per_cpu}G",
        "enable_hyper_threading": _boolean(
            intent.get("enable_hyper_threading"), field_name="enable_hyper_threading"
        ),
        "entrypoint": entrypoint,
    }
    if intent.get("memory_per_node") not in (None, ""):
        sbatch_script["memory_per_node"] = (
            f"{_positive_int(intent['memory_per_node'], field_name='memory_per_node')}G"
        )
    if intent.get("job_max_time") not in (None, ""):
        sbatch_script["job_max_time"] = str(intent["job_max_time"])

    cpu_info = offer.raw.get("cpu_info")
    if not isinstance(cpu_info, Mapping):
        cpu_info = {}
    gpu_info = offer.raw.get("gpu_info")
    if not isinstance(gpu_info, Mapping):
        gpu_info = {}
    spec_price: dict[str, Any] = {
        "cpu_type": str(cpu_info.get("cpu_type") or offer.raw.get("cpu_type") or ""),
        "cpu_count": offer.cpu_count,
        "gpu_count": offer.gpu_count,
        "memory_size_gib": offer.memory_size_gib,
        "logic_compute_group_id": offer.logic_compute_group_id,
        "quota_id": offer.quota_id,
    }
    if offer.gpu_count > 0:
        gpu_type = str(
            gpu_info.get("gpu_type") or offer.raw.get("gpu_type") or offer.gpu_type or ""
        ).strip()
        if not gpu_type:
            raise ConfigError("Selected GPU Scheduling Offer lacks a machine-readable gpu_type")
        spec_price["gpu_type"] = gpu_type
    payload: dict[str, Any] = {
        "job_name": name,
        "logic_compute_group_id": offer.logic_compute_group_id,
        "project_id": project.project_id,
        "workspace_id": workspace_id,
        "enable_notification": False,
        "sbatch_script": sbatch_script,
        "slurm_cluster_spec": {
            "predef_quota_id": offer.quota_id,
            "cpu": offer.cpu_count,
            "mem_gi": offer.memory_size_gib,
            "image": image.url,
            "image_type": image.source,
            "instance_count": instance_count,
            "spec_price": spec_price,
        },
    }
    if intent.get("priority") not in (None, ""):
        payload["priority"] = _integer(intent["priority"], field_name="priority")

    resolved = {
        "workspace": {"intent": workspace_label, "id": workspace_id},
        "project": {"intent": intent["project"], "id": project.project_id, "name": project.name},
        "image": {
            "intent": intent["image"],
            "id": image.image_id,
            "url": image.url,
            "source": image.source,
            "status": image.status,
        },
    }
    preview_intent = {**intent, "name": name, "entrypoint": entrypoint}
    preview = LaunchPreview(
        kind="hpc",
        account_context={
            "alias": config.account_alias,
            "username": config.username,
            "base_url": config.base_url,
        },
        intent=preview_intent,
        sources={**sources, "name": "cli", "entrypoint": "cli"},
        resolved=resolved,
        scheduling_offer=_offer_payload(offer),
        availability=availability,
        platform_request=payload,
        warnings=warnings,
    )
    return PreparedWorkloadLaunch.build(
        kind="hpc", preview=preview, platform_payload=payload, session=session
    )


def _worker_intents(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    workers = intent.get("workers")
    if workers in (None, []):
        return [
            {
                "name": "workers",
                "group": intent.get("group"),
                "quota": intent.get("quota"),
                "image": intent.get("image"),
                "image_type": intent.get("image_type"),
                "min_replicas": 1,
                "max_replicas": 2,
            }
        ]
    if not isinstance(workers, Sequence) or isinstance(workers, (str, bytes)):
        raise ConfigError("ray workers must be a list of objects")
    result: list[dict[str, Any]] = []
    for index, worker in enumerate(workers):
        if not isinstance(worker, Mapping):
            raise ConfigError(f"ray workers[{index}] must be an object")
        merged = {
            "group": intent.get("group"),
            "quota": intent.get("quota"),
            "image": intent.get("image"),
            "image_type": intent.get("image_type"),
            **dict(worker),
        }
        result.append(merged)
    if not result:
        raise ConfigError("ray workers must contain at least one worker group")
    return result


def prepare_ray_launch(
    *,
    config: Config,
    name: str,
    entrypoint: str,
    profile_name: str | None = None,
    explicit: Mapping[str, Any],
    explicit_workspace_id: str | None = None,
    session: WebSession | None = None,
) -> PreparedWorkloadLaunch:
    """Resolve exact live bindings and build the native Ray create payload."""
    name = str(name or "").strip()
    entrypoint = str(entrypoint or "").strip()
    if not name:
        raise ConfigError("Ray launch requires a non-empty name")
    if not entrypoint:
        raise ConfigError("Ray launch requires a non-empty entrypoint")
    (
        intent,
        sources,
        workspace_id,
        workspace_label,
        project,
        discovery,
        head_offer,
        head_image,
        session,
        availability,
        warnings,
    ) = _base_resolution(
        kind="ray",
        config=config,
        profile_name=profile_name,
        explicit=explicit,
        explicit_workspace_id=explicit_workspace_id,
        session=session,
    )
    head_node: dict[str, Any] = {
        "mirror_id": head_image.image_id,
        "image_type": head_image.source,
        "logic_compute_group_id": head_offer.logic_compute_group_id,
        "quota_id": head_offer.quota_id,
        "shm_gi": _positive_int(
            (
                (intent.get("head") or {}).get("shm_size")
                if isinstance(intent.get("head"), Mapping)
                else None
            ),
            field_name="head.shm_size",
            default=32,
        ),
    }

    worker_groups: list[dict[str, Any]] = []
    worker_resolved: list[dict[str, Any]] = []
    worker_offers: list[dict[str, Any]] = []
    for index, worker in enumerate(_worker_intents(intent)):
        worker_group = _required_text(worker, "group", kind=f"ray worker {index}")
        worker_quota = _required_text(worker, "quota", kind=f"ray worker {index}")
        worker_image_intent = _required_text(worker, "image", kind=f"ray worker {index}")
        offer = _resolve_offer(discovery, group=worker_group, quota=worker_quota)
        image = _resolve_image(
            worker_image_intent,
            workspace_id=workspace_id,
            session=session,
        )
        min_replicas = _positive_int(
            worker.get("min_replicas"), field_name=f"workers[{index}].min_replicas", default=1
        )
        max_replicas = _positive_int(
            worker.get("max_replicas"), field_name=f"workers[{index}].max_replicas", default=2
        )
        if max_replicas < min_replicas:
            raise ConfigError(f"workers[{index}].max_replicas must be >= min_replicas")
        group_name = str(worker.get("name") or worker.get("group_name") or f"workers-{index}")
        block: dict[str, Any] = {
            "group_name": group_name,
            "mirror_id": image.image_id,
            "image_type": image.source,
            "logic_compute_group_id": offer.logic_compute_group_id,
            "quota_id": offer.quota_id,
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
        }
        if worker.get("shm_size") not in (None, ""):
            block["shm_gi"] = _positive_int(
                worker["shm_size"], field_name=f"workers[{index}].shm_size"
            )
        worker_groups.append(block)
        worker_offers.append(_offer_payload(offer))
        worker_resolved.append(
            {
                "name": group_name,
                "image": {
                    "intent": worker_image_intent,
                    "id": image.image_id,
                    "url": image.url,
                    "source": image.source,
                    "status": image.status,
                },
            }
        )

    payload: dict[str, Any] = {
        "name": name,
        "description": str(intent.get("description") or ""),
        "workspace_id": workspace_id,
        "project_id": project.project_id,
        "task_priority": _integer(intent.get("priority"), field_name="priority", default=4),
        "entrypoint": entrypoint,
        "head_node": head_node,
        "worker_groups": worker_groups,
    }
    preview_intent = {**intent, "name": name, "entrypoint": entrypoint}
    preview = LaunchPreview(
        kind="ray",
        account_context={
            "alias": config.account_alias,
            "username": config.username,
            "base_url": config.base_url,
        },
        intent=preview_intent,
        sources={**sources, "name": "cli", "entrypoint": "cli"},
        resolved={
            "workspace": {"intent": workspace_label, "id": workspace_id},
            "project": {
                "intent": intent["project"],
                "id": project.project_id,
                "name": project.name,
            },
            "head_image": {
                "intent": intent["image"],
                "id": head_image.image_id,
                "url": head_image.url,
                "source": head_image.source,
                "status": head_image.status,
            },
            "workers": worker_resolved,
        },
        scheduling_offer={"head": _offer_payload(head_offer), "workers": worker_offers},
        availability=availability,
        platform_request=payload,
        warnings=warnings,
    )
    return PreparedWorkloadLaunch.build(
        kind="ray", preview=preview, platform_payload=payload, session=session
    )


def submit_prepared_launch(prepared: PreparedWorkloadLaunch) -> dict[str, Any]:
    """Perform the single mutation represented by a prepared launch."""
    if not isinstance(prepared, PreparedWorkloadLaunch):
        raise TypeError("submission requires PreparedWorkloadLaunch")
    if prepared.kind == "hpc":
        return browser_api.create_hpc_job(prepared.request_dict(), session=prepared.session)
    if prepared.kind == "ray":
        return browser_api.create_ray_job(prepared.request_dict(), session=prepared.session)
    raise ValueError(f"Unsupported prepared workload kind: {prepared.kind}")


__all__ = [
    "PreparedWorkloadLaunch",
    "prepare_hpc_launch",
    "prepare_ray_launch",
    "submit_prepared_launch",
]
