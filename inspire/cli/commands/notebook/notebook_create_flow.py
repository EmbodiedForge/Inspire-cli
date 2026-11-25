"""Notebook creation flow for `inspire notebook create`."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Optional

import click

from inspire.cli.commands.notebook.group_capability import (
    OFFERS_COMMAND,
    NotebookGroupCapability,
    fetch_notebook_group_capability,
)
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_USAGE_ERROR,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.datasets import (
    DATASET_REFERENCE_HINT,
    DatasetValidationError,
    dataset_mount_path,
    parse_dataset_target,
    validate_dataset_targets,
    validation_pair_key,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.launch_preview import redact_structure
from inspire.cli.utils.notebook_cli import load_config, require_web_session, resolve_json_output
from inspire.cli.utils.notebook_post_start import (
    NotebookPostStartSpec,
    NO_WAIT_POST_START_WARNING,
    POST_START_MODE_RUN,
    parse_post_start_exit_code,
    resolve_notebook_post_start_spec,
)
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.config.projects import resolve_project_request
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.browser_api.availability.models import GPUAvailability
from inspire.platform.web.browser_api.images import CustomImageInfo
from inspire.platform.web.browser_api.notebooks import ImageInfo
from inspire.platform.web.browser_api.priority import (
    PRIORITY_LEVEL_BANDS,
    allowed_priority_levels_for_offer,
    priority_can_preempt,
    priority_matches_allowed,
)
from inspire.platform.web.browser_api.workload_contracts import WorkloadOffer
from inspire.platform.web.resources import is_schedulable_resource_pool
from inspire.platform.web.session import WebSession

NotebookImage = ImageInfo | CustomImageInfo

_CPU_ALIASES = {"CPU", "CPUONLY", "CPU_ONLY", "CPU-ONLY"}


def _node_task_count(node: dict) -> int:
    tasks = node.get("tasks_associated")
    if isinstance(tasks, dict):
        try:
            return int(tasks.get("count") or 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(tasks, list):
        return len(tasks)
    task_list = node.get("task_list")
    if isinstance(task_list, list):
        return len(task_list)
    return 0


def _parse_resource_with_pattern(
    resource: str,
    pattern: str,
    *,
    with_count_and_type: bool = False,
) -> tuple[int, str, Optional[int]] | None:
    match = re.match(pattern, resource)
    if not match:
        return None

    if with_count_and_type:
        count = int(match.group(1))
        resource_type = match.group(2)
        if resource_type in _CPU_ALIASES:
            return 0, "CPU", count
        return count, resource_type, None

    count = int(match.group(1))
    return count, "GPU", None


def parse_resource_string(resource: str) -> tuple[int, str, Optional[int]]:
    resource = resource.strip().upper()

    for pattern in (r"^(\d+)\s*[xX]$", r"^(\d+)$"):
        parsed = _parse_resource_with_pattern(resource, pattern)
        if parsed is not None:
            return parsed

    for pattern in (r"^(\d+)\s*[xX]\s*(\w+)$", r"^(\d+)\s+(\w+)$", r"^(\d+)([A-Z0-9_-]+)$"):
        parsed = _parse_resource_with_pattern(
            resource,
            pattern,
            with_count_and_type=True,
        )
        if parsed is not None:
            return parsed

    match = re.match(r"^(\w+)$", resource)
    if match:
        pattern = match.group(1)
        if pattern in _CPU_ALIASES:
            return 0, "CPU", None
        return 1, pattern, None

    raise ValueError(f"Invalid resource format: {resource}")


def format_resource_display(gpu_count: int, gpu_pattern: str, cpu_count: Optional[int]) -> str:
    if gpu_count == 0 and gpu_pattern.upper() == "CPU":
        if cpu_count:
            return f"{cpu_count}xCPU"
        return "CPU"
    return f"{gpu_count}x{gpu_pattern}"


def match_gpu_type(pattern: str, gpu_type_display: str) -> bool:
    pattern_norm = re.sub(r"[^A-Z0-9]+", "", (pattern or "").upper())
    display_norm = re.sub(r"[^A-Z0-9]+", "", (gpu_type_display or "").upper())
    if not pattern_norm or not display_norm:
        return False
    if pattern_norm in display_norm or display_norm in pattern_norm:
        return True

    pattern_simple = _simple_gpu_type_label(pattern_norm)
    display_simple = _simple_gpu_type_label(display_norm)
    return bool(pattern_simple and display_simple and pattern_simple == display_simple)


def _simple_gpu_type_label(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "", (value or "").upper())
    for token in ("H200", "H100", "4090", "A100", "A800"):
        if token in normalized:
            return token
    return ""


_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"


def resolve_notebook_workspace_id(
    ctx: Context,
    *,
    config: Config,
    session: WebSession,
    workspace: Optional[str],
    workspace_id: Optional[str],
    gpu_count: int,
    gpu_pattern: str,
) -> str | None:
    try:
        auto_workspace_id = (
            select_workspace_id(
                config,
                explicit_workspace_id=workspace_id,
                explicit_workspace_name=workspace,
                session=session,
            )
            or config.notebook_workspace_id
            or config.default_workspace_id
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return None

    if not auto_workspace_id:
        auto_workspace_id = session.workspace_id

    if auto_workspace_id == _ZERO_WORKSPACE_ID:
        auto_workspace_id = None

    if not auto_workspace_id:
        _handle_error(
            ctx,
            "ConfigError",
            "No workspace_id configured.",
            EXIT_CONFIG_ERROR,
            hint="Use --workspace-id, --workspace, or [defaults].workspace_id.",
        )
        return None

    return auto_workspace_id


def _auto_select_compute_group(
    ctx: Context,
    *,
    session: WebSession,
    gpu_count: int,
    gpu_pattern: str,
    auto: bool,
    json_output: bool,
    workspace_id: str,
    priority: Optional[int] = None,
    resource_prices_by_group: Optional[dict[str, list[dict]]] = None,
) -> tuple[GPUAvailability | None, str, str] | None:
    if not (auto and gpu_count > 0):
        return None, "", gpu_pattern

    filter_gpu_type = None if gpu_pattern == "GPU" else gpu_pattern
    try:
        discovery = browser_api_module.list_notebook_offers(
            workspace_id=workspace_id,
            session=session,
            gpu_type=filter_gpu_type,
        )
        compatible_offers = [
            offer
            for offer in discovery.offers
            if priority_matches_allowed(priority, getattr(offer, "allowed_priority_levels", ()))
        ]
        matching_offers = [
            offer
            for offer in compatible_offers
            if offer.gpu_count == gpu_count
            and (filter_gpu_type is None or match_gpu_type(gpu_pattern, offer.gpu_type))
        ]
        if not matching_offers:
            requested = format_resource_display(gpu_count, gpu_pattern, None)
            _handle_error(
                ctx,
                "AvailabilityError",
                f"No launchable notebook offer matches {requested}",
                EXIT_CONFIG_ERROR,
                hint=(
                    f"Inspect live offers with: inspire notebook offers "
                    f"--workspace-id {workspace_id}"
                ),
            )
            return None

        offers_by_group: dict[str, list[WorkloadOffer]] = {}
        selected_offer_by_group: dict[str, WorkloadOffer] = {}
        for offer in compatible_offers:
            offers_by_group.setdefault(offer.logic_compute_group_id, []).append(offer)
        for offer in matching_offers:
            selected_offer_by_group.setdefault(offer.logic_compute_group_id, offer)

        candidate_groups = [
            {
                "logic_compute_group_id": group_id,
                "name": offer.logic_compute_group_name,
                "gpu_type": offer.gpu_type,
            }
            for group_id, offer in selected_offer_by_group.items()
        ]
        request_specs_by_group = {
            group_id: {
                "gpu_count": int(offer.gpu_count),
                "cpu_count": int(offer.cpu_count),
                "memory_size_gib": int(offer.memory_size_gib),
            }
            for group_id, offer in selected_offer_by_group.items()
        }
        best = browser_api_module.find_best_compute_group_accurate(
            gpu_type=filter_gpu_type,
            min_gpus=gpu_count,
            include_preemptible=priority_can_preempt(priority),
            # A single GPU cannot be fragmented, so group-wide node totals are
            # sufficient. Multi-GPU requests require one node that fits the
            # exact GPU/CPU/memory offer.
            prefer_full_nodes=gpu_count > 1,
            workspace_id=workspace_id,
            candidate_groups=candidate_groups,
            request_specs_by_group=request_specs_by_group if gpu_count > 1 else None,
        )
    except Exception as e:
        _handle_error(
            ctx,
            "AvailabilityError",
            f"Auto-select failed: {e}",
            EXIT_CONFIG_ERROR,
        )
        return None

    if not best:
        requested = format_resource_display(gpu_count, gpu_pattern, None)
        message = (
            f"No launchable compute group has capacity for {requested}"
            if gpu_count == 1
            else f"No launchable compute group has a node that fits {requested}"
        )
        _handle_error(
            ctx,
            "AvailabilityError",
            message,
            EXIT_CONFIG_ERROR,
        )
        return None

    if resource_prices_by_group is not None:
        resource_prices_by_group[best.group_id] = [
            dict(offer.raw) for offer in offers_by_group.get(best.group_id, [])
        ]

    if gpu_pattern == "GPU":
        gpu_pattern = _simple_gpu_type_label(best.gpu_type) or best.gpu_type or "GPU"
    auto_selected_gpu_type = best.gpu_type or ""

    if not json_output:
        source = getattr(best, "selection_source", "")
        if source == "nodes-fit" and best.free_nodes:
            click.echo(
                f"Auto-selected: {best.group_name}, "
                f"{best.free_nodes} request-fit node(s) ({best.available_gpus} GPUs)"
            )
        elif source.startswith("nodes") and best.free_nodes:
            click.echo(
                f"Auto-selected: {best.group_name}, "
                f"{best.free_nodes} full node(s) free ({best.available_gpus} GPUs)"
            )
        elif source.startswith("nodes") and getattr(best, "low_priority_nodes_estimate", 0):
            click.echo(
                f"Auto-selected: {best.group_name}, {best.free_nodes} full node(s) free "
                f"(+{best.low_priority_nodes_estimate} low-priority node estimate)"
            )
        else:
            click.echo(f"Auto-selected: {best.group_name}, {best.available_gpus} GPUs available")
        fallback_reason = getattr(best, "fallback_reason", "")
        if fallback_reason:
            click.echo(f"Node-level availability fallback: {fallback_reason}")

    return best, auto_selected_gpu_type, gpu_pattern


def _match_compute_group_by_name_or_id(
    *,
    compute_groups: list[dict],
    name_or_id: str,
) -> dict | None:
    """Find a compute group by exact ID or substring name match."""
    name_lower = name_or_id.lower()
    for group in compute_groups:
        if group.get("logic_compute_group_id") == name_or_id:
            return group
    for group in compute_groups:
        for field in ("name", "compute_group_name"):
            group_name = group.get(field, "")
            if group_name and name_lower in group_name.lower():
                return group
    return None


def _group_is_bound_to_workspace(group: dict, workspace_id: str) -> bool:
    workspace_ids = group.get("workspace_ids", [])
    if isinstance(workspace_ids, str):
        workspace_ids = [workspace_ids] if workspace_ids else []
    elif not isinstance(workspace_ids, list):
        workspace_ids = []
    return workspace_id in workspace_ids


def _filter_workspace_bound_groups(*, compute_groups: list[dict], workspace_id: str) -> list[dict]:
    return [group for group in compute_groups if _group_is_bound_to_workspace(group, workspace_id)]


def _resolve_group_gpu_type(
    *,
    group: dict,
    workspace_id: str,
    session: WebSession,
    gpu_count: int,
    gpu_pattern: str,
) -> str:
    def _matches_pattern(candidate: str) -> bool:
        return bool(candidate) and (gpu_pattern == "GPU" or match_gpu_type(gpu_pattern, candidate))

    gpu_type_stats = group.get("gpu_type_stats", [])
    for gpu_stats in gpu_type_stats:
        gpu_info = gpu_stats.get("gpu_info", {})
        gpu_type = gpu_info.get("gpu_type", "") or ""
        gpu_type_display = gpu_info.get("gpu_type_display", "") or ""
        if _matches_pattern(gpu_type):
            return gpu_type
        if _matches_pattern(gpu_type_display):
            return gpu_type or gpu_type_display

    logic_compute_group_id = group.get("logic_compute_group_id", "")
    if not logic_compute_group_id:
        return ""

    try:
        resource_prices = browser_api_module.get_resource_prices(
            workspace_id=workspace_id,
            logic_compute_group_id=logic_compute_group_id,
            session=session,
        )
    except Exception:
        return ""

    for price_entry in resource_prices:
        if price_entry.get("gpu_count", 0) != gpu_count:
            continue
        gpu_info = price_entry.get("gpu_info", {})
        gpu_type = gpu_info.get("gpu_type", "") or ""
        if _matches_pattern(gpu_type):
            return gpu_type

    return ""


def _match_compute_group_by_gpu_type(
    *,
    compute_groups: list[dict],
    gpu_pattern: str,
) -> tuple[dict | None, str]:
    for group in compute_groups:
        gpu_stats_list = group.get("gpu_type_stats", [])
        for gpu_stats in gpu_stats_list:
            gpu_info = gpu_stats.get("gpu_info", {})
            gpu_type_display = gpu_info.get("gpu_type_display", "")
            if match_gpu_type(gpu_pattern, gpu_type_display):
                return group, gpu_info.get("gpu_type", "")
    for group in compute_groups:
        if not group.get("gpu_type_stats"):
            for field in ("name", "compute_group_name"):
                group_name = group.get(field, "")
                if group_name and match_gpu_type(gpu_pattern, group_name):
                    return group, ""
    return None, ""


def _match_cpu_only_compute_group(
    compute_groups: list[dict],
    *,
    workspace_id: str = "",
    session: Optional[WebSession] = None,
    requested_cpu_count: Optional[int] = None,
    runnable_group_ids: Optional[set[str]] = None,
) -> tuple[dict | None, str]:
    """Select the best CPU-only compute group.

    Two-pass selection:
    1. Keep only the groups ``runnable_group_ids`` allows, when the caller has that
       answer. Specs and free nodes decide which eligible group is *better*; they
       cannot decide whether the platform will accept one at all, which is why #113
       reached the create as ``已选择的计算类型组不支持创建交互式建模实例``.
    2. Probe workspace-bound groups via browser API node/spec data.
    3. Prefer groups with matching CPU specs and more free CPU nodes.
    4. Use CPU-ish names only as a final tiebreaker between equally viable groups.
    5. Fail closed when CPU capability cannot be positively confirmed.
    """
    candidates = (
        _filter_workspace_bound_groups(compute_groups=compute_groups, workspace_id=workspace_id)
        if workspace_id
        else list(compute_groups)
    )
    if runnable_group_ids is not None:
        candidates = [
            group
            for group in candidates
            if group.get("logic_compute_group_id") in runnable_group_ids
        ]

    if not candidates or not workspace_id or session is None:
        return None, ""

    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _cpu_name_score(group: dict) -> int:
        for field in ("name", "compute_group_name"):
            value = (group.get(field) or "").strip().upper()
            if not value:
                continue
            if value.startswith("CPU"):
                return 1
        return 0

    def _node_dimension_counts(group_id: str) -> dict[str, float]:
        counts = {
            "fit_nodes": 0.0,
            "ready_nodes": 0.0,
            "total_nodes": 0.0,
            "available_cpu": 0.0,
            "free_nodes": 0.0,
        }
        requested_cpu = float(requested_cpu_count or 1)
        try:
            data = web_session_module.fetch_node_dimensions(
                session,
                base_url=getattr(session, "base_url", "") or "https://api.example.com",
                workspace_id=workspace_id,
                logic_compute_group_id=group_id,
            )
        except Exception:
            return counts

        nodes = data.get("data", {}).get("node_dimensions", []) or []
        for node in nodes:
            counts["total_nodes"] += 1
            status = str(node.get("status") or "").upper()
            cordon_type = str(node.get("cordon_type") or "").strip()
            is_maint = bool(node.get("is_maint", False))
            resource_pool = str(node.get("resource_pool") or "").lower()
            if (
                status != "READY"
                or cordon_type
                or is_maint
                or not is_schedulable_resource_pool(resource_pool)
            ):
                continue

            counts["ready_nodes"] += 1
            cpu_available = _safe_float((node.get("cpu") or {}).get("available"))
            counts["available_cpu"] += max(cpu_available, 0.0)
            if cpu_available >= requested_cpu:
                counts["fit_nodes"] += 1
            if _node_task_count(node) == 0:
                counts["free_nodes"] += 1

        return counts

    groups_with_resources: list[dict] = []
    for group in candidates:
        gid = group.get("logic_compute_group_id", "")
        if not gid:
            continue
        try:
            prices = browser_api_module.get_resource_prices(
                workspace_id=workspace_id,
                logic_compute_group_id=gid,
                session=session,
            )
        except Exception:
            continue

        cpu_prices = [p for p in prices if p.get("gpu_count", 0) == 0]
        if cpu_prices:
            dimension_counts = _node_dimension_counts(gid)
            groups_with_resources.append(
                {
                    "group": group,
                    "requested_match": requested_cpu_count is None
                    or any(
                        int(price.get("cpu_count", 0) or 0) == requested_cpu_count
                        for price in cpu_prices
                    ),
                    "fit_nodes": dimension_counts["fit_nodes"],
                    "available_cpu": dimension_counts["available_cpu"],
                    "free_nodes": dimension_counts["free_nodes"],
                    "ready_nodes": dimension_counts["ready_nodes"],
                    "total_nodes": dimension_counts["total_nodes"],
                    "cpu_name_score": _cpu_name_score(group),
                }
            )

    if requested_cpu_count is not None:
        groups_with_resources = [
            entry for entry in groups_with_resources if entry["requested_match"]
        ]

    if not groups_with_resources:
        return None, ""

    groups_with_resources.sort(
        key=lambda entry: (
            entry["fit_nodes"],
            entry["available_cpu"],
            entry["free_nodes"],
            entry["ready_nodes"],
            entry["total_nodes"],
            entry["cpu_name_score"],
        ),
        reverse=True,
    )
    return groups_with_resources[0]["group"], ""


def _describe_capability_gap(
    capability: NotebookGroupCapability,
    *,
    requested_cpu_count: Optional[int] = None,
) -> str:
    """Say, in CLI terms, why nothing here can run the notebook.

    The platform answers the same situation with ``已选择的计算类型组不支持创建交互式建模实例，
    请点击"编辑"更新计算类型组`` - advice for whoever configures the console, addressed
    to someone trying to start a notebook. This is the CLI's own version: which
    groups were refused, what was asked for, and the two commands that can act on it.
    """
    lines: list[str] = []
    offered_sizes = capability.offered_cpu_sizes()
    if requested_cpu_count is not None and offered_sizes:
        matching = [
            offer
            for offer in capability.offers_for_size(requested_cpu_count)
            if offer.logic_compute_group_name
        ]
        if not matching:
            lines.append(
                f"No workload-capable group offers {requested_cpu_count}xCPU; "
                f"offered CPU sizes: {', '.join(str(size) for size in offered_sizes)}"
            )
    blocked = capability.groups_with_quota_but_no_capability
    if blocked:
        lines.append("Groups with quota rows that cannot run an interactive notebook:")
        lines.extend(
            f"  - {row.get('logic_compute_group_name') or row.get('logic_compute_group_id')}"
            f" ({row.get('logic_compute_group_id')})"
            for row in blocked
        )
    without_rows = capability.groups_without_quota_rows()
    if without_rows:
        lines.append("Groups that can run one but have no quota rows:")
        lines.extend(
            f"  - {group.get('logic_compute_group_name') or group.get('logic_compute_group_id')}"
            for group in without_rows
        )
    lines.append(
        f"Inspect the candidates: {OFFERS_COMMAND} --workspace-id {capability.workspace_id}"
    )
    lines.append(
        "Choose one deliberately: inspire notebook create --compute-group <name-or-id> "
        f"({OFFERS_COMMAND} shows which groups can run a notebook)"
    )
    return "\n".join(lines)


def _build_compute_group_hint(
    *,
    compute_groups: list[dict],
    gpu_count: int,
    workspace_id: str = "",
    session: Optional[WebSession] = None,
    requested_cpu_count: Optional[int] = None,
    capability: Optional[NotebookGroupCapability] = None,
) -> str | None:
    if gpu_count == 0 and capability is not None and capability.known:
        return _describe_capability_gap(capability, requested_cpu_count=requested_cpu_count)

    if gpu_count == 0 and workspace_id and session is not None:
        try:
            discovery = browser_api_module.list_notebook_offers(
                workspace_id=workspace_id,
                session=session,
            )
            cpu_offers = [offer for offer in discovery.offers if offer.gpu_count == 0]
            matching_offers = [
                offer
                for offer in cpu_offers
                if requested_cpu_count is None or offer.cpu_count == requested_cpu_count
            ]
            if matching_offers:
                return None
            if cpu_offers:
                specs = sorted(
                    {
                        (offer.cpu_count, offer.memory_size_gib)
                        for offer in cpu_offers
                        if offer.cpu_count is not None
                    }
                )
                formatted = "\n".join(
                    f"  - {cpu_count}xCPU / {memory_size or '?'} GiB"
                    for cpu_count, memory_size in specs
                )
                return (
                    "No launchable CPU notebook offer matches the requested size.\n"
                    f"Available CPU notebook offers:\n{formatted}"
                )
            if discovery.capability_groups_without_quota_rows:
                groups = "\n".join(
                    f"  - {group.get('logic_compute_group_name') or group.get('logic_compute_group_id')}"
                    for group in discovery.capability_groups_without_quota_rows
                )
                return (
                    "Notebook-capable compute groups are visible, but the platform returned no "
                    "DSW quota rows for them. They are not launchable notebook offers:\n"
                    f"{groups}\n"
                    f"Inspect with: inspire notebook offers --workspace-id {workspace_id}"
                )
        except Exception:
            pass

    available_types: set[str] = set()
    for group in compute_groups:
        for stats in group.get("gpu_type_stats", []):
            gpu_type = stats.get("gpu_info", {}).get("gpu_type_display", "Unknown")
            if gpu_type:
                available_types.add(gpu_type)
    if not available_types:
        return None
    formatted = "\n".join(f"  - {gpu_type}" for gpu_type in sorted(available_types))
    return f"Available resource types:\n{formatted}"


def resolve_notebook_compute_group(
    ctx: Context,
    *,
    session: WebSession,
    workspace_id: str,
    gpu_count: int,
    gpu_pattern: str,
    requested_cpu_count: Optional[int],
    auto: bool,
    json_output: bool,
    compute_group_name: Optional[str] = None,
    task_priority: Optional[int] = None,
    resource_prices_by_group: Optional[dict[str, list[dict]]] = None,
    capability: Optional[NotebookGroupCapability] = None,
) -> tuple[str, str, str, str, str] | None:
    auto_selected_group, auto_selected_gpu_type = None, ""
    if compute_group_name and gpu_count > 0 and gpu_pattern == "GPU":
        _handle_error(
            ctx,
            "ValidationError",
            "Explicit --compute-group requires a typed GPU resource",
            EXIT_CONFIG_ERROR,
            hint="Use a typed GPU resource such as '1xH200' or '1x4090' with --compute-group.",
        )
        return None

    if not compute_group_name:
        auto_selected = _auto_select_compute_group(
            ctx,
            session=session,
            gpu_count=gpu_count,
            gpu_pattern=gpu_pattern,
            auto=auto,
            json_output=json_output,
            workspace_id=workspace_id,
            priority=task_priority,
            resource_prices_by_group=resource_prices_by_group,
        )
        if auto_selected is None:
            return None
        auto_selected_group, auto_selected_gpu_type, gpu_pattern = auto_selected

    resource_display = format_resource_display(gpu_count, gpu_pattern, requested_cpu_count)

    if not json_output:
        click.echo(f"Creating notebook with {resource_display}...")

    selected_group: dict[str, Any] | None = None
    selected_gpu_type = ""
    compute_groups: list[dict[str, Any]] = []

    # Auto-selection already queried this exact workspace and returned a live
    # compute-group ID. Preserve that decision through quota lookup and the
    # create payload instead of re-listing groups and potentially substituting
    # another group with the same GPU type.
    if auto_selected_group:
        auto_group_id = str(getattr(auto_selected_group, "group_id", "") or "").strip()
        if auto_group_id:
            selected_group = {
                "logic_compute_group_id": auto_group_id,
                "name": str(getattr(auto_selected_group, "group_name", "") or auto_group_id),
                "workspace_ids": [workspace_id],
            }
            selected_gpu_type = auto_selected_gpu_type or str(
                getattr(auto_selected_group, "gpu_type", "") or ""
            )

    if selected_group is None:
        try:
            compute_groups = browser_api_module.list_notebook_compute_groups(
                workspace_id=workspace_id,
                session=session,
            )
        except Exception as e:
            _handle_error(ctx, "APIError", f"Error fetching compute groups: {e}", EXIT_API_ERROR)
            return None

    if compute_group_name:
        selected_group = _match_compute_group_by_name_or_id(
            compute_groups=compute_groups,
            name_or_id=compute_group_name,
        )
        if not selected_group:
            hint: Optional[str] = "Available groups:\n" + "\n".join(
                f"  - {g.get('name', g.get('compute_group_name', '?'))}" for g in compute_groups
            )
            _handle_error(
                ctx,
                "ValidationError",
                f"Compute group '{compute_group_name}' not found",
                EXIT_CONFIG_ERROR,
                hint=hint,
            )
            return None
        if not selected_group.get("workspace_ids"):
            _handle_error(
                ctx,
                "ValidationError",
                (f"Compute group '{compute_group_name}' is missing workspace binding metadata"),
                EXIT_CONFIG_ERROR,
                hint=(
                    "Run discovery again to populate compute-group workspace_ids, "
                    "or configure this group's workspace_ids explicitly."
                ),
            )
            return None
        if not _group_is_bound_to_workspace(selected_group, workspace_id):
            bound_ids = ", ".join(selected_group.get("workspace_ids") or [])
            _handle_error(
                ctx,
                "ValidationError",
                (
                    f"Compute group '{compute_group_name}' is not bound to workspace "
                    f"'{workspace_id}'"
                ),
                EXIT_CONFIG_ERROR,
                hint=(
                    f"Choose a compute group bound to {workspace_id}. "
                    f"Current bindings: {bound_ids or '(none)'}"
                ),
            )
            return None

        # An override chooses among the groups the platform will accept; it is not a
        # licence to name one it refuses. Anything `--compute-group` named used to be
        # sent, and for a group with quota rows but no interactive_modeling
        # capability the platform answered with a console-configuration error the CLI
        # had no business relaying (#113). Offering that group is a dead end, so it is
        # refused as an unusable option value rather than warned about.
        if capability is None:
            capability = fetch_notebook_group_capability(workspace_id=workspace_id, session=session)
        override_id = str(selected_group.get("logic_compute_group_id") or "")
        if capability.known and override_id not in (capability.runnable_group_ids() or set()):
            blocked = next(
                (
                    row
                    for row in capability.groups_with_quota_but_no_capability
                    if str(row.get("logic_compute_group_id") or "") == override_id
                ),
                None,
            )
            if blocked is not None:
                row_count = blocked.get("row_count")
                counted = f"{row_count} quota row(s)" if row_count else "quota rows"
                fact = (
                    f"The platform lists {counted} for it but no interactive_modeling "
                    "capability, so a create here is answered with "
                    "已选择的计算类型组不支持创建交互式建模实例."
                )
            else:
                fact = (
                    "The platform does not report it among the groups that can run an "
                    "interactive notebook."
                )
            _handle_error(
                ctx,
                "ValidationError",
                f"Compute group '{compute_group_name}' cannot run an interactive notebook",
                EXIT_USAGE_ERROR,
                hint=(
                    f"{fact} Run {OFFERS_COMMAND} --workspace-id {workspace_id} for the "
                    "groups that can run one, and pass one of those to --compute-group."
                ),
            )
            return None

        if gpu_count == 0:
            logic_compute_group_id = selected_group.get("logic_compute_group_id", "")
            try:
                resource_prices = browser_api_module.get_resource_prices(
                    workspace_id=workspace_id,
                    logic_compute_group_id=logic_compute_group_id,
                    session=session,
                )
            except Exception as e:
                _handle_error(
                    ctx,
                    "APIError",
                    f"Failed to probe CPU notebook specs for compute group '{compute_group_name}': {e}",
                    EXIT_API_ERROR,
                )
                return None

            cpu_prices = [p for p in resource_prices if p.get("gpu_count", 0) == 0]
            if requested_cpu_count is not None:
                cpu_prices = [
                    p for p in cpu_prices if int(p.get("cpu_count", 0) or 0) == requested_cpu_count
                ]
            if not cpu_prices:
                requested_label = (
                    f"{requested_cpu_count}xCPU" if requested_cpu_count is not None else "CPU"
                )
                _handle_error(
                    ctx,
                    "ValidationError",
                    (
                        f"Compute group '{compute_group_name}' does not expose notebook CPU specs "
                        f"for '{requested_label}'"
                    ),
                    EXIT_CONFIG_ERROR,
                    hint=(
                        "Run 'inspire resources list' and choose a CPU group with "
                        "Specs=yes. Groups shown as Specs=no cannot be used for CPU notebook creation."
                    ),
                )
                return None
        if gpu_count > 0:
            selected_gpu_type = _resolve_group_gpu_type(
                group=selected_group,
                workspace_id=workspace_id,
                session=session,
                gpu_count=gpu_count,
                gpu_pattern=gpu_pattern,
            )
            if not selected_gpu_type:
                _handle_error(
                    ctx,
                    "ValidationError",
                    (
                        f"Compute group '{compute_group_name}' does not match requested "
                        f"GPU resource '{resource_display}'"
                    ),
                    EXIT_CONFIG_ERROR,
                    hint="Use a typed GPU resource that matches the selected compute group, or choose a compute group with explicit matching GPU metadata.",
                )
                return None

    if not selected_group and gpu_count > 0:
        selected_group, selected_gpu_type = _match_compute_group_by_gpu_type(
            compute_groups=_filter_workspace_bound_groups(
                compute_groups=compute_groups, workspace_id=workspace_id
            ),
            gpu_pattern=gpu_pattern,
        )
    if not selected_group and gpu_count == 0:
        # The API listing may not return all CPU compute groups.  Merge in
        # config-based groups so resource probing can find groups the API missed.
        api_ids = {g.get("logic_compute_group_id") for g in compute_groups}
        try:
            from inspire.platform.web.browser_api.notebooks import (
                _config_compute_groups_fallback,
            )

            config_groups = _config_compute_groups_fallback(workspace_id=workspace_id)
            for cg in config_groups:
                if cg.get("logic_compute_group_id") not in api_ids:
                    compute_groups.append(cg)
        except Exception:
            pass
        # The offer data, not the spec probe, decides which groups are candidates.
        # A group whose quota rows come with no workload capability can hold every
        # spec and every free node in the workspace and still refuse the create, so
        # it is dropped before anything is ranked (#113). Where no offer data could
        # be read at all, `runnable_group_ids` is None and the old ranking runs.
        if capability is None:
            # Read here rather than only at the call site: a caller that forgets the
            # fact is the same bug wearing a different name. `run_notebook_create`
            # passes its own read in, so this costs nothing on the real path.
            capability = fetch_notebook_group_capability(workspace_id=workspace_id, session=session)
        runnable_group_ids = capability.runnable_group_ids()
        if capability is not None:
            listed_ids = {group.get("logic_compute_group_id") for group in compute_groups}
            for offer in capability.offers_for_size(None):
                group_id = offer.logic_compute_group_id
                if not group_id or group_id in listed_ids:
                    continue
                # An offering group is launchable by definition, even where the
                # listing missed it; the GPU auto path trusts the same data.
                compute_groups.append(
                    {
                        "logic_compute_group_id": group_id,
                        "name": offer.logic_compute_group_name or group_id,
                        "workspace_ids": [workspace_id],
                    }
                )
                listed_ids.add(group_id)

        selected_group, selected_gpu_type = _match_cpu_only_compute_group(
            compute_groups,
            workspace_id=workspace_id,
            session=session,
            requested_cpu_count=requested_cpu_count,
            runnable_group_ids=runnable_group_ids,
        )
        if not selected_group and runnable_group_ids is not None:
            _handle_error(
                ctx,
                "ValidationError",
                (
                    f"No compute group in workspace '{workspace_id}' can run a "
                    f"{resource_display} notebook"
                ),
                EXIT_CONFIG_ERROR,
                hint=_describe_capability_gap(capability, requested_cpu_count=requested_cpu_count),
            )
            return None

    if not selected_group:
        hint = _build_compute_group_hint(
            compute_groups=compute_groups,
            gpu_count=gpu_count,
            workspace_id=workspace_id,
            session=session,
            requested_cpu_count=requested_cpu_count,
            capability=capability,
        )
        if gpu_count == 0 and not hint:
            hint = (
                f"The selected workspace (ID: {workspace_id[:20]}...) does not have any "
                f"CPU-capable compute groups available.\n"
                f"This account may not have access to CPU compute resources, or the CPU "
                f"workspace is misconfigured.\n\n"
                f"To troubleshoot:\n"
                f"  1. Check available compute groups: inspire resources list\n"
                f"  2. Verify workspace configuration: inspire config show\n"
                f"  3. Try a different workspace: inspire notebook create --workspace <name> -r 4CPU\n"
                f"  4. Contact your platform administrator to verify CPU quota/assignment"
            )
        _handle_error(
            ctx,
            "ValidationError",
            f"No compute group found with resource type matching '{gpu_pattern}'",
            EXIT_CONFIG_ERROR,
            hint=hint,
        )
        return None

    logic_compute_group_id = selected_group.get("logic_compute_group_id")
    if not logic_compute_group_id:
        _handle_error(
            ctx,
            "APIError",
            "Selected compute group is missing logic_compute_group_id",
            EXIT_API_ERROR,
        )
        return None

    selected_group_name = (
        selected_group.get("name")
        or selected_group.get("compute_group_name")
        or logic_compute_group_id
    )

    if resource_prices_by_group is not None and capability is not None:
        # Carry the chosen group's own offer rows into the quota and spec lookups
        # that follow. An offer row is the only place the platform binds a group to
        # a quota id, and the spec lookup rejects a price row whose quota id
        # disagrees with the one it was handed - so a quota sourced from anywhere
        # else can only answer for a different group (#113's `quota_id: ''`).
        group_key = str(logic_compute_group_id)
        offer_rows = capability.resource_price_rows(group_key)
        if offer_rows and group_key not in resource_prices_by_group:
            resource_prices_by_group[group_key] = offer_rows

    return (
        logic_compute_group_id,
        selected_gpu_type,
        gpu_pattern,
        resource_display,
        selected_group_name,
    )


def _cpu_offer_row(
    resource_prices: Optional[list[dict]],
    requested_cpu_count: Optional[int],
) -> Optional[dict]:
    """The chosen group's own CPU quota row, or None when there is no such row.

    A workspace schedule lists quotas; only the group's resource-price row binds a
    quota to *this* group. The GPU branch below has read that binding from the price
    rows all along, and ``resolve_notebook_resource_spec_price`` then rejects any
    price row whose ``quota_id`` disagrees with the one it was handed - so a CPU
    quota taken from the schedule alone either names another group's quota or, as in
    #113, arrives empty and matches nothing.
    """
    candidates: list[dict] = []
    for row in resource_prices or []:
        if int(row.get("gpu_count", 0) or 0) != 0:
            continue
        if not str(row.get("quota_id") or ""):
            continue
        row_cpu = row.get("cpu_count")
        if requested_cpu_count is not None and row_cpu != requested_cpu_count:
            continue
        candidates.append(row)
    if not candidates:
        return None
    return min(candidates, key=lambda row: int(row.get("cpu_count") or 0))


def resolve_notebook_quota(
    ctx: Context,
    *,
    schedule: dict,
    gpu_count: int,
    gpu_pattern: str,
    requested_cpu_count: Optional[int],
    selected_gpu_type: str,
    resource_prices: Optional[list[dict]] = None,
) -> tuple[str, int, int, str, str] | None:
    quota_list = schedule.get("quota", [])
    if isinstance(quota_list, str):
        quota_list = json.loads(quota_list) if quota_list else []

    selected_quota = None
    cpu_quotas: list[dict] = []
    offer_row = _cpu_offer_row(resource_prices, requested_cpu_count) if gpu_count == 0 else None
    if gpu_count == 0:
        cpu_quotas = [q for q in quota_list if q.get("gpu_count", 0) == 0]
        if requested_cpu_count is None:
            for quota in cpu_quotas:
                quota_cpu = quota.get("cpu_count")
                if quota_cpu is None:
                    continue
                if selected_quota is None or quota_cpu < selected_quota.get("cpu_count", 0):
                    selected_quota = quota
            if selected_quota is None and cpu_quotas:
                selected_quota = cpu_quotas[0]
        else:
            for quota in cpu_quotas:
                if quota.get("cpu_count") == requested_cpu_count:
                    selected_quota = quota
                    break
    else:
        # Current platform schedule quotas no longer carry gpu_type (only a
        # display name like "8卡160核"). The type-to-quota mapping now lives in
        # the selected compute group's resource prices, which pair gpu_info
        # with quota_id — resolve the quota through them first.
        price_quota_ids: list[str] = []
        for price_entry in resource_prices or []:
            if price_entry.get("gpu_count", 0) != gpu_count:
                continue
            entry_gpu_type = price_entry.get("gpu_info", {}).get("gpu_type", "")
            if (
                gpu_pattern
                and gpu_pattern != "GPU"
                and not match_gpu_type(gpu_pattern, entry_gpu_type)
            ):
                continue
            entry_quota_id = price_entry.get("quota_id", "")
            if entry_quota_id and entry_quota_id not in price_quota_ids:
                price_quota_ids.append(entry_quota_id)
        for quota_id_candidate in price_quota_ids:
            for quota in quota_list:
                if quota.get("gpu_count") != gpu_count:
                    continue
                if quota.get("id", "") == quota_id_candidate:
                    selected_quota = quota
                    break
            if selected_quota:
                break
        if selected_quota is None:
            for quota in quota_list:
                if quota.get("gpu_count") != gpu_count:
                    continue
                quota_gpu_type = quota.get("gpu_type", "")
                if quota_gpu_type and match_gpu_type(gpu_pattern, quota_gpu_type):
                    selected_quota = quota
                    break

    if selected_quota is None and offer_row is not None:
        # The workspace schedule has nothing for this request, but the group we
        # placed the notebook on answers it with a quota of its own.
        selected_quota = {
            "id": str(offer_row.get("quota_id") or ""),
            "cpu_count": offer_row.get("cpu_count", requested_cpu_count or 20),
            "memory_size": offer_row.get("memory_size_gib", 200),
            "gpu_type": "",
        }

    if not selected_quota:
        if gpu_count == 0:
            if not quota_list:
                requested_label = (
                    f"{requested_cpu_count}xCPU" if requested_cpu_count is not None else "CPU"
                )
                _handle_error(
                    ctx,
                    "ValidationError",
                    f"No CPU quota data returned for {requested_label}",
                    EXIT_CONFIG_ERROR,
                    hint=(
                        "CPU notebook creation requires browser schedule quota data. "
                        "Run 'inspire resources list' and choose a CPU group with Specs=yes."
                    ),
                )
                return None
            requested_label = (
                f"{requested_cpu_count}xCPU" if requested_cpu_count is not None else "CPU"
            )
            message = f"No quota found for {requested_label}"

            lines: list[str] = []
            for quota in cpu_quotas:
                quota_cpu = quota.get("cpu_count")
                quota_name = quota.get("name")
                label = f"{quota_cpu}xCPU" if quota_cpu else "CPU"
                suffix = f" ({quota_name})" if quota_name else ""
                lines.append(f"  - {label}{suffix}")

            hint = "Available CPU quotas:\n" + "\n".join(lines) if lines else None
        else:
            # When the schedule API is unavailable, quota_list will be empty.
            # Use reasonable defaults so notebook creation can proceed.
            if not quota_list:
                cpu_count = requested_cpu_count or 20
                memory_size = 200
                resource_display = format_resource_display(gpu_count, gpu_pattern, cpu_count)
                return "", cpu_count, memory_size, selected_gpu_type, resource_display
            message = f"No quota found for {gpu_count}x {gpu_pattern}"

            lines = []
            for quota in quota_list:
                quota_name = quota.get("name")
                gpu_label = quota.get("gpu_type") or "GPU"
                suffix = f" ({quota_name})" if quota_name else ""
                lines.append(f"  - {quota.get('gpu_count')}x {gpu_label}{suffix}")

            hint = "Available quotas:\n" + "\n".join(lines) if lines else None

        _handle_error(ctx, "ValidationError", message, EXIT_CONFIG_ERROR, hint=hint)
        return None

    quota_id = selected_quota.get("id", "")
    cpu_count = selected_quota.get("cpu_count", 20)
    memory_size = selected_quota.get("memory_size", 200)

    if gpu_count == 0 and offer_row is not None and str(offer_row.get("quota_id") or ""):
        # The group's own row wins over the schedule's id even when both are
        # present: the two lists describe the same shape from different sides, and
        # only one of them is read again by the spec lookup, which drops every row
        # whose quota id does not match what it was given.
        quota_id = str(offer_row.get("quota_id"))

    if gpu_count == 0:
        selected_gpu_type = selected_quota.get("gpu_type", "") or ""
    else:
        quota_gpu_type = selected_quota.get("gpu_type", "") or ""
        if quota_gpu_type:
            selected_gpu_type = quota_gpu_type

    resource_display = format_resource_display(gpu_count, gpu_pattern, cpu_count)

    return quota_id, cpu_count, memory_size, selected_gpu_type, resource_display


def resolve_notebook_project(
    ctx: Context,
    *,
    projects: list,
    config: Config,
    project: str | None,
    allow_requested_over_quota: bool,
    needs_gpu_quota: bool,
    json_output: bool,
    workspace_id: str | None = None,
    session: WebSession | None = None,
) -> object | None:
    try:
        project_value = (
            resolve_project_request(
                config,
                project,
                workspace_id=str(workspace_id or ""),
                projects=projects,
                session=session,
            )
            if workspace_id and session
            else project
        )
        shared_groups = getattr(config, "project_shared_path_groups", None)
        if not isinstance(shared_groups, dict) or not shared_groups:
            shared_groups = None

        congested: set[str] | None = None
        if needs_gpu_quota and workspace_id and session:
            congested = (
                browser_api_module.check_scheduling_health(
                    workspace_id=workspace_id,
                    project_ids={p.project_id for p in projects},
                    session=session,
                )
                or None
            )

        selected_project, fallback_msg = browser_api_module.select_project(
            projects,
            project_value,
            allow_requested_over_quota=allow_requested_over_quota,
            shared_path_group_by_id=shared_groups,
            needs_gpu_quota=needs_gpu_quota,
            project_order=config.project_order or None,
            congested_projects=congested,
        )

        if not json_output:
            if fallback_msg:
                click.echo(fallback_msg)
            click.echo(
                "Using project: "
                f"{selected_project.name}{selected_project.get_quota_status(needs_gpu=needs_gpu_quota)}"
            )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            hint = None
            if projects:
                hint = "Available projects:\n" + "\n".join(f"  - {p.name}" for p in projects)
            _handle_error(ctx, "ValidationError", error_msg, EXIT_CONFIG_ERROR, hint=hint)
            return None
        _handle_error(ctx, "QuotaExceeded", error_msg, EXIT_CONFIG_ERROR)
        return None

    return selected_project


def _find_image_match(images: list[NotebookImage], image: str) -> NotebookImage | None:
    """Find an image matching the query string (case-insensitive name/URL or exact ID)."""
    image_lower = image.lower()
    for img in images:
        if (
            image_lower in img.name.lower()
            or image_lower in img.url.lower()
            or img.image_id == image
        ):
            return img
    return None


def resolve_notebook_image(
    ctx: Context,
    *,
    images: list[NotebookImage],
    image: Optional[str],
    json_output: bool,
) -> NotebookImage | None:
    selected_image = None

    if image:
        selected_image = _find_image_match(images, image)
        if not selected_image:
            hint = "Available images:\n" + "\n".join(f"  - {img.name}" for img in images[:20])
            _handle_error(
                ctx,
                "ValidationError",
                f"Image '{image}' not found",
                EXIT_CONFIG_ERROR,
                hint=hint,
            )
            return None
    else:
        if not json_output:
            click.echo("\nAvailable images:")
            for i, img in enumerate(images[:10], 1):
                click.echo(f"  [{i}] {img.name}")
            if len(images) > 10:
                click.echo(f"  ... and {len(images) - 10} more")

            default_idx = 1
            for i, img in enumerate(images, 1):
                if "pytorch" in img.name.lower():
                    default_idx = i
                    break

            try:
                choice = click.prompt("\nSelect image", type=int, default=default_idx)
                if choice < 1 or choice > len(images):
                    _handle_error(
                        ctx,
                        "ValidationError",
                        "Invalid selection",
                        EXIT_CONFIG_ERROR,
                        hint=f"Choose between 1 and {len(images)}.",
                    )
                    return None
                selected_image = images[choice - 1]
            except click.Abort:
                _handle_error(ctx, "Aborted", "Aborted.", EXIT_CONFIG_ERROR)
                return None
        else:
            for img in images:
                if "pytorch" in img.name.lower():
                    selected_image = img
                    break
            if not selected_image:
                selected_image = images[0]

    return selected_image


def resolve_notebook_resource_spec_price(
    ctx: Context,
    *,
    resource_prices: list[dict],
    gpu_count: int,
    selected_gpu_type: str,
    gpu_pattern: str,
    logic_compute_group_id: str,
    quota_id: str,
    cpu_count: int,
    memory_size: int,
    requested_cpu_count: Optional[int],
) -> tuple[dict, str, int, int] | None:
    if gpu_count == 0:
        cpu_spec = {
            "cpu_type": "",
            "cpu_count": cpu_count,
            "gpu_type": "",
            "gpu_count": 0,
            "memory_size_gib": memory_size,
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": quota_id,
        }
        matched = False

        for price_entry in resource_prices:
            if price_entry.get("gpu_count", 0) != 0:
                continue

            entry_quota_id = price_entry.get("quota_id", "")
            entry_cpu_count = price_entry.get("cpu_count")

            if quota_id and entry_quota_id and entry_quota_id != quota_id:
                continue
            if requested_cpu_count is not None and entry_cpu_count != requested_cpu_count:
                continue

            matched = True
            entry_cpu_info = price_entry.get("cpu_info", {})
            cpu_type = entry_cpu_info.get("cpu_type", "")
            if cpu_type:
                cpu_spec["cpu_type"] = cpu_type
            if not quota_id and entry_quota_id:
                quota_id = entry_quota_id
                cpu_spec["quota_id"] = entry_quota_id
            break

        if not matched:
            requested_label = (
                f"{requested_cpu_count}xCPU" if requested_cpu_count is not None else "CPU"
            )
            _handle_error(
                ctx,
                "ValidationError",
                f"No notebook CPU resource spec found for {requested_label}",
                EXIT_CONFIG_ERROR,
                hint=(
                    "The selected compute group did not return a matching CPU notebook spec. "
                    "Run 'inspire resources list' and choose a CPU group with Specs=yes."
                ),
            )
            return None

        return cpu_spec, quota_id, cpu_count, memory_size

    resource_spec_price = {
        "cpu_type": "",
        "cpu_count": cpu_count,
        "gpu_type": selected_gpu_type or "",
        "gpu_count": gpu_count,
        "memory_size_gib": memory_size,
        "logic_compute_group_id": logic_compute_group_id,
        "quota_id": quota_id,
    }

    for price_entry in resource_prices:
        entry_gpu_count = price_entry.get("gpu_count", 0)
        entry_gpu_info = price_entry.get("gpu_info", {})
        entry_gpu_type = entry_gpu_info.get("gpu_type", "")
        entry_quota_id = price_entry.get("quota_id", "")
        entry_cpu_info = price_entry.get("cpu_info", {})

        if entry_gpu_count != gpu_count:
            continue
        if not match_gpu_type(gpu_pattern, entry_gpu_type):
            continue
        if quota_id and entry_quota_id and entry_quota_id != quota_id:
            continue

        resource_spec_price = {
            "cpu_type": entry_cpu_info.get("cpu_type", ""),
            "cpu_count": price_entry.get("cpu_count", cpu_count),
            "gpu_type": entry_gpu_type,
            "gpu_count": entry_gpu_count,
            "memory_size_gib": price_entry.get("memory_size_gib", memory_size),
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": entry_quota_id or quota_id,
        }
        if not quota_id:
            quota_id = entry_quota_id
        cpu_count = price_entry.get("cpu_count", cpu_count)
        mem_gib = price_entry.get("memory_size_gib")
        if mem_gib is not None:
            memory_size = mem_gib
        break

    return resource_spec_price, quota_id, cpu_count, memory_size


def parse_dataset_references(
    ctx: Context, dataset: tuple[str, ...]
) -> Optional[list[tuple[str, str]]]:
    """Split every ``--dataset CODE@VERSION`` value, or report the first unusable one.

    None is the finding, not an empty result: no ``--dataset`` at all is a valid empty list, while
    a malformed reference is bad input (12) and costs nothing beyond the message - which is why this
    runs before any session is opened.
    """
    pairs: list[tuple[str, str]] = []
    for raw in dataset:
        try:
            pairs.append(parse_dataset_target(raw))
        except ValueError as exc:
            _handle_error(
                ctx,
                "ValidationError",
                str(exc),
                EXIT_VALIDATION_ERROR,
                hint=DATASET_REFERENCE_HINT,
            )
            return None
    return pairs


def resolve_notebook_dataset_info(
    ctx: Context,
    *,
    pairs: list[tuple[str, str]],
    workspace_id: str,
    session: WebSession,
    json_output: bool,
) -> Optional[list[dict[str, str]]]:
    """Resolve requested dataset versions into the create body's ``dataset_info``, or None to stop.

    This is the console's own pre-flight: it asks the platform whether every version mounts in the
    workspace that will host the notebook, and one refusal stops the creation rather than starting
    an instance with half its data missing. A refusal is a platform verdict (13) rather than bad
    input (12) - the reference was well-formed, the platform simply said no - and its wording is
    kept verbatim because it names the reason.
    """
    try:
        dataset_info = validate_dataset_targets(pairs, workspace_id, session)
    except DatasetValidationError as exc:
        _handle_error(
            ctx,
            "APIError",
            f"Refusing to create notebook: {exc}",
            EXIT_API_ERROR,
            hint=(
                "Check what will mount: "
                f"inspire dataset check {exc.dataset_code}@{exc.version_code} "
                f"--workspace-id {workspace_id}"
            ),
        )
        return None
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", f"Failed to validate datasets: {exc}", EXIT_API_ERROR)
        return None

    if not json_output:
        for entry in dataset_info:
            click.echo(
                f"Using dataset: {entry['dataset_id']}@{entry['version_id']} -> "
                f"{dataset_mount_path(entry['dataset_id'], entry['version_id'])}"
            )
    return dataset_info


def _build_create_preview(
    *,
    name: str,
    resource_display: str,
    auto_stop: bool,
    shm_size: int,
    task_priority: Optional[int],
    workspace_id: str,
    selected_project,
    selected_group_name: str,
    logic_compute_group_id: str,
    quota_id: str,
    selected_gpu_type: str,
    gpu_count: int,
    cpu_count: int,
    memory_size: int,
    selected_image,
    resource_spec_price: Optional[dict],
    dataset_info: Optional[list[dict]] = None,
    warnings: Optional[list[str]] = None,
) -> dict:
    """The would-be notebook create, resolved and redacted, with no mutation performed.

    ``session`` is the one create_notebook argument left out: it carries cookies, not request data.
    ``dataset_info`` follows the same rule as the create body and is absent rather than empty.
    ``warnings`` carries what the resolved fields do not say on their own; a preview
    that resolved a group and printed nothing was the lie in #113.
    """
    platform_request = {
        "name": name,
        "project_id": selected_project.project_id,
        "project_name": selected_project.name,
        "image_id": selected_image.image_id,
        "image_url": selected_image.url,
        "logic_compute_group_id": logic_compute_group_id,
        "quota_id": quota_id,
        "gpu_type": selected_gpu_type,
        "gpu_count": gpu_count,
        "cpu_count": cpu_count,
        "memory_size": memory_size,
        "shared_memory_size": shm_size,
        "auto_stop": auto_stop,
        "workspace_id": workspace_id,
        "task_priority": task_priority,
        "resource_spec_price": resource_spec_price,
    }
    if dataset_info is not None:
        platform_request["dataset_info"] = dataset_info
    return {
        "kind": "notebook",
        "mutation": "skipped",
        "intent": {
            "name": name,
            "resource": resource_display,
            "project": selected_project.name,
            "group": selected_group_name,
            "image": selected_image.name,
            "shm_size": shm_size,
            "auto_stop": auto_stop,
            "priority": task_priority,
            "datasets": [
                f"{entry['dataset_id']}@{entry['version_id']} -> "
                f"{dataset_mount_path(entry['dataset_id'], entry['version_id'])}"
                for entry in dataset_info or []
            ],
        },
        "resolved": {
            "workspace_id": workspace_id,
            "project_id": selected_project.project_id,
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": quota_id,
            "image_id": selected_image.image_id,
            "image_url": selected_image.url,
        },
        "platform_request": redact_structure(platform_request),
        "warnings": list(warnings or []),
    }


def _render_create_preview(preview: dict) -> None:
    intent = preview["intent"]
    resolved = preview["resolved"]
    click.echo("Notebook Create Preview\n")
    click.echo(f"Name:       {intent['name']}")
    click.echo(f"Resource:   {intent['resource']}")
    click.echo(f"Workspace:  {resolved['workspace_id']}")
    click.echo(f"Project:    {intent['project']}")
    click.echo(f"Group:      {intent['group']}")
    click.echo(f"Quota:      {resolved['quota_id']}")
    click.echo(f"Image:      {intent['image']}")
    priority = intent.get("priority")
    if priority is not None:
        priority_display = str(priority)
        try:
            if int(priority) <= 3:
                priority_display = f"{priority_display} (LOW, preemptible)"
        except (TypeError, ValueError):
            pass
        click.echo(f"Priority:   {priority_display}")
    for dataset in intent.get("datasets") or []:
        click.echo(f"Dataset:    {dataset}")
    for warning in preview.get("warnings", []):
        click.echo(f"Warning:    {warning}")
    click.echo("Mutation:   skipped")
    click.echo("No notebook was created.")


class NotebookDatasetMountNotAppliedError(Exception):
    """The platform accepted a dataset-bearing notebook and then attached none of it.

    The notebook-side twin of ``DatasetMountNotAppliedError`` (#80): the create surface
    accepts ``dataset_info`` and answers 200 either way, so the created notebook is read
    back once and refused when its mount list does not cover what was submitted.
    """

    def __init__(self, notebook_id: str, missing: list[str], submitted: int) -> None:
        self.notebook_id = notebook_id
        self.missing = missing
        self.submitted = submitted
        super().__init__(
            f"Notebook {notebook_id} was created with {submitted} dataset version(s), but the "
            f"platform reports these as not attached: {', '.join(missing)}. Stop the notebook "
            "before it runs against missing input."
        )


def _notebook_dataset_pair_of(entry: Any) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        return None
    dataset_id = entry.get("dataset_id") or entry.get("dataset_code") or entry.get("name")
    version_id = entry.get("version_id") or entry.get("version_code") or entry.get("version")
    if not dataset_id or not version_id:
        return None
    return validation_pair_key(dataset_id, version_id)


def verify_notebook_datasets(
    *,
    notebook_id: str,
    dataset_info: list[dict[str, str]],
    session: WebSession,
) -> None:
    """Read the created notebook back and refuse it unless every submitted version is attached."""
    detail = browser_api_module.get_notebook_detail(notebook_id=notebook_id, session=session)
    mounted = detail.get("dataset_info") if isinstance(detail, dict) else None
    mounted = mounted if isinstance(mounted, list) else []

    mounted_pairs = {_notebook_dataset_pair_of(entry) for entry in mounted}
    mounted_pairs.discard(None)
    missing = [
        f"{entry.get('dataset_id')}@{entry.get('version_id')}"
        for entry in dataset_info
        if _notebook_dataset_pair_of(entry) not in mounted_pairs
    ]
    if missing:
        raise NotebookDatasetMountNotAppliedError(notebook_id, missing, len(dataset_info))


def _emit_create_json_success(
    *,
    notebook_id: str,
    name: str,
    resource_display: str,
    selected_project,
    selected_image,
    logic_compute_group_id: str,
    compute_group_name: str,
    workspace_id: str,
    session: WebSession,
) -> None:
    workspace_name = (getattr(session, "all_workspace_names", None) or {}).get(workspace_id, "")
    click.echo(
        json_formatter.format_json(
            {
                "notebook_id": notebook_id,
                "name": name,
                "resource": resource_display,
                "project": selected_project.name,
                "image": selected_image.name,
                "logic_compute_group_id": logic_compute_group_id,
                "compute_group_name": compute_group_name,
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
            }
        )
    )


def create_notebook_and_report(
    ctx: Context,
    *,
    name: str,
    resource_display: str,
    selected_project,
    selected_image,
    logic_compute_group_id: str,
    compute_group_name: str,
    quota_id: str,
    selected_gpu_type: str,
    gpu_count: int,
    cpu_count: int,
    memory_size: int,
    shm_size: int,
    auto_stop: bool,
    workspace_id: str,
    session: WebSession,
    json_output: bool,
    task_priority: Optional[int] = None,
    resource_spec_price: Optional[dict] = None,
    dataset_info: Optional[list[dict]] = None,
    defer_json_report: bool = False,
) -> str | None:
    notebook_id = ""
    try:
        result = browser_api_module.create_notebook(
            name=name,
            project_id=selected_project.project_id,
            project_name=selected_project.name,
            image_id=selected_image.image_id,
            image_url=selected_image.url,
            logic_compute_group_id=logic_compute_group_id,
            quota_id=quota_id,
            gpu_type=selected_gpu_type,
            gpu_count=gpu_count,
            cpu_count=cpu_count,
            memory_size=memory_size,
            shared_memory_size=shm_size,
            auto_stop=auto_stop,
            workspace_id=workspace_id,
            session=session,
            task_priority=task_priority,
            resource_spec_price=resource_spec_price,
            dataset_info=dataset_info,
        )

        notebook_id = result.get("notebook_id", "")

        if dataset_info and notebook_id:
            # The create surface accepts dataset_info and answers 200 either way; read the
            # notebook back once so a silent drop surfaces now, not in a failed run (#80).
            verify_notebook_datasets(
                notebook_id=notebook_id,
                dataset_info=dataset_info,
                session=session,
            )

        if json_output:
            if not defer_json_report:
                _emit_create_json_success(
                    notebook_id=notebook_id,
                    name=name,
                    resource_display=resource_display,
                    selected_project=selected_project,
                    selected_image=selected_image,
                    logic_compute_group_id=logic_compute_group_id,
                    compute_group_name=compute_group_name,
                    workspace_id=workspace_id,
                    session=session,
                )
        else:
            click.echo("\nNotebook created successfully!")
            click.echo(f"  ID: {notebook_id}")
            click.echo(f"  Name: {name}")
            click.echo(f"  Resource: {resource_display}")
            if task_priority is not None:
                priority_display = str(task_priority)
                try:
                    if int(task_priority) <= 3:
                        priority_display = f"{priority_display} (LOW, preemptible)"
                except (TypeError, ValueError):
                    pass
                click.echo(f"  Priority: {priority_display}")

        return notebook_id

    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to create notebook: {e}",
            EXIT_API_ERROR,
            data={"notebook_id": notebook_id} if notebook_id else None,
        )
        return None


def maybe_wait_for_running(
    ctx: Context,
    *,
    notebook_id: str,
    session: WebSession,
    wait: bool,
    needs_post_start: bool,
    json_output: bool,
    timeout: int = 600,
) -> bool:
    if not (wait or needs_post_start):
        return True

    if needs_post_start and not wait and not json_output:
        click.echo(NO_WAIT_POST_START_WARNING, err=True)

    if not json_output:
        click.echo("Waiting for notebook to reach RUNNING status...")

    try:
        browser_api_module.wait_for_notebook_running(
            notebook_id=notebook_id,
            session=session,
            timeout=timeout,
        )
        if not json_output:
            click.echo("Notebook is now RUNNING.")
        return True
    except NotebookFailedError as e:
        msg = f"Notebook failed to start: {e}"
        hint_parts = []
        if e.events:
            hint_parts.append(e.events)
        extra = e.detail.get("extra_info") or {}
        for key in ("NodeName", "HostIP"):
            if extra.get(key):
                hint_parts.append(f"{key}: {extra[key]}")
        if not hint_parts:
            hint_parts.append(
                "Check Events tab in web UI: Jobs > Interactive Modeling > notebook detail"
            )
        _handle_error(
            ctx,
            "NotebookFailed",
            msg,
            EXIT_API_ERROR,
            hint="\n".join(hint_parts),
            data={"notebook_id": notebook_id},
        )
        return False
    except TimeoutError as e:
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook to reach RUNNING: {e}",
            EXIT_API_ERROR,
            data={"notebook_id": notebook_id},
        )
        return False


def maybe_run_post_start(
    ctx: Context,
    *,
    notebook_id: str,
    session: WebSession,
    post_start_spec: NotebookPostStartSpec | None,
    gpu_count: int,
    json_output: bool,
) -> None:
    if post_start_spec is None:
        return
    if post_start_spec.requires_gpu and gpu_count <= 0:
        return

    if not json_output:
        action = "Running" if post_start_spec.mode == POST_START_MODE_RUN else "Starting"
        click.echo(f"{action} {post_start_spec.label}...")

    try:
        result = browser_api_module.run_command_in_notebook(
            notebook_id=notebook_id,
            command=post_start_spec.command,
            session=session,
            timeout=600 if post_start_spec.mode == POST_START_MODE_RUN else 20,
            completion_marker=post_start_spec.completion_marker,
        )
        success = bool(result.get("success")) if isinstance(result, dict) else bool(result)
        if post_start_spec.mode != POST_START_MODE_RUN:
            if not json_output and success:
                click.echo(f"{post_start_spec.label} started (log: {post_start_spec.log_path})")
                click.echo(
                    f'  To stop: inspire bridge exec "kill $(cat {post_start_spec.pid_file})"'
                )
            if not json_output and not success:
                click.echo(
                    f"Warning: Failed to confirm {post_start_spec.label.lower()} startup; check "
                    f"{post_start_spec.log_path} inside the notebook.",
                    err=True,
                )
            return

        if not success:
            error = result.get("error") if isinstance(result, dict) else None
            message = f"Failed to complete {post_start_spec.label}."
            if error:
                message = f"{message} {error}"
            _handle_error(
                ctx,
                "PostStartFailed",
                message,
                EXIT_API_ERROR,
                hint=f"Check {post_start_spec.log_path} inside the notebook for output.",
                data={"notebook_id": notebook_id},
            )
            return

        stdout = str(result.get("stdout") or "") if isinstance(result, dict) else ""
        try:
            exit_code = parse_post_start_exit_code(stdout, post_start_spec.completion_marker)
        except RuntimeError as exc:
            _handle_error(
                ctx,
                "PostStartFailed",
                str(exc),
                EXIT_API_ERROR,
                hint=f"Check {post_start_spec.log_path} inside the notebook for output.",
                data={"notebook_id": notebook_id},
            )
            return

        if exit_code != 0:
            _handle_error(
                ctx,
                "PostStartFailed",
                f"{post_start_spec.label} exited with status {exit_code}.",
                EXIT_API_ERROR,
                hint=f"Check {post_start_spec.log_path} inside the notebook for output.",
                data={"notebook_id": notebook_id},
            )
            return

        if not json_output:
            click.echo(f"{post_start_spec.label} completed (log: {post_start_spec.log_path})")
    except Exception as e:
        if post_start_spec.mode == POST_START_MODE_RUN:
            _handle_error(
                ctx,
                "PostStartFailed",
                f"Failed to complete {post_start_spec.label}: {e}",
                EXIT_API_ERROR,
                hint=f"Check {post_start_spec.log_path} inside the notebook for output.",
                data={"notebook_id": notebook_id},
            )
            return
        if not json_output:
            click.echo(
                f"Warning: Failed to start {post_start_spec.label.lower()}: {e}",
                err=True,
            )


def _resolve_create_inputs(
    *,
    config: Config,
    resource: str | None,
    project: str | None,
    image: str | None,
    shm_size: int | None,
) -> tuple[str, str | None, str | None, int]:
    if not resource:
        resource = config.notebook_resource or getattr(config, "default_resource", None) or "1xH200"
    if not project:
        project = getattr(config, "notebook_project_id", None)
    if not image:
        image = config.notebook_image or getattr(config, "default_image", None)
    if shm_size is None:
        notebook_shm_size = getattr(config, "notebook_shm_size", None)
        if notebook_shm_size is not None:
            shm_size = notebook_shm_size
        else:
            shm_size = config.shm_size if config.shm_size is not None else 32
    if shm_size < 1:
        raise ValueError("Shared memory size must be >= 1.")
    return resource, project, image, shm_size


def _fetch_notebook_schedule(
    ctx: Context,
    *,
    workspace_id: str,
    session: WebSession,
) -> dict | None:
    try:
        return browser_api_module.get_notebook_schedule(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch notebook schedule: {e}", EXIT_API_ERROR)
        return None


def _fetch_resource_prices(
    *,
    workspace_id: str,
    logic_compute_group_id: str,
    session: WebSession,
    json_output: bool,
) -> list[dict]:
    if not logic_compute_group_id:
        return []

    try:
        return browser_api_module.get_resource_prices(
            workspace_id=workspace_id,
            logic_compute_group_id=logic_compute_group_id,
            session=session,
        )
    except Exception as e:
        if not json_output:
            click.echo(f"Warning: Failed to fetch resource prices: {e}", err=True)
        return []


def _resolve_task_priority(priority: Optional[int], config: Config) -> Optional[int]:
    if priority is not None:
        return priority
    notebook_priority = getattr(config, "notebook_priority", None)
    if notebook_priority is not None:
        return notebook_priority
    default_priority = getattr(config, "default_priority", None)
    if default_priority is not None:
        return default_priority
    return 6


def _fetch_workspace_projects(
    ctx: Context,
    *,
    workspace_id: str,
    session: WebSession,
) -> list | None:
    try:
        projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch projects: {e}", EXIT_API_ERROR)
        return None

    if projects:
        return projects

    _handle_error(ctx, "ConfigError", "No projects available in this workspace", EXIT_CONFIG_ERROR)
    return None


def _cap_task_priority(
    *,
    task_priority: Optional[int],
    selected_project,
    json_output: bool,
) -> Optional[int]:
    if not selected_project.priority_name:
        return task_priority

    try:
        max_priority = int(selected_project.priority_name)
    except ValueError:
        return task_priority

    if task_priority is None or task_priority <= max_priority:
        return task_priority

    if not json_output:
        click.echo(
            f"Capping priority {task_priority} → {max_priority} "
            f"(max for project '{selected_project.name}')"
        )
    return max_priority


def _find_quota_allowed_priority_levels(
    schedule: dict,
    *,
    group_id: str,
    quota_id: str,
) -> list[str]:
    """Levels one group/quota offer accepts, in the platform's own ordering.

    A quota id is shared by several compute groups, so picking the first schedule row with a
    matching id drops the limits declared for the group actually being created in. The index in
    `priority.py` keys by group first and keeps a quota-only fallback row, which is the ordering
    every other offer surface (`job quota`, training resource selection) already uses.
    """
    return list(allowed_priority_levels_for_offer(schedule, group_id=group_id, quota_id=quota_id))


def _apply_allowed_priority_levels(
    *,
    task_priority: Optional[int],
    allowed_levels: list[str],
    json_output: bool,
) -> Optional[int]:
    """Snap the task priority into the quota's allowed priority bands.

    Current platform quotas may restrict which priority levels a resource
    spec accepts (e.g. training-area H200 quotas only allow ``low``);
    sending a priority outside the allowed bands fails with API error
    200008. An empty ``allowed_levels`` list means unrestricted.
    """
    if not allowed_levels or task_priority is None:
        return task_priority
    bands = [
        PRIORITY_LEVEL_BANDS[level] for level in allowed_levels if level in PRIORITY_LEVEL_BANDS
    ]
    if not bands:
        return task_priority
    if any(low <= task_priority <= high for low, high in bands):
        return task_priority

    adjusted = min(
        (boundary for low, high in bands for boundary in (low, high)),
        key=lambda boundary: (abs(boundary - task_priority), boundary < task_priority),
    )
    if not json_output:
        click.echo(
            f"Adjusting priority {task_priority} → {adjusted} "
            f"(quota allows: {', '.join(allowed_levels)})"
        )
    return adjusted


def _fetch_notebook_images(
    ctx: Context,
    *,
    workspace_id: str,
    session: WebSession,
    image: Optional[str],
    json_output: bool,
) -> list[NotebookImage] | None:
    try:
        images: list[NotebookImage] = list(
            browser_api_module.list_images(workspace_id=workspace_id, session=session)
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch images: {e}", EXIT_API_ERROR)
        return None

    # Fetch public images when searching for a specific image or for interactive listing
    if not image or (image and not _find_image_match(images, image)):
        try:
            public_images = browser_api_module.list_images(
                workspace_id=workspace_id, source="SOURCE_PUBLIC", session=session
            )
            if public_images:
                if image and not json_output:
                    click.echo("Searching public images...")
                images.extend(public_images)
        except Exception:
            pass

    # Fetch personal-visible images when searching for a specific image or for interactive listing
    if not image or (image and not _find_image_match(images, image)):
        try:
            personal_images = browser_api_module.list_images_by_source(
                source="personal-visible", workspace_id=workspace_id, session=session
            )
            if personal_images:
                if image and not json_output:
                    click.echo("Searching personal images...")
                images.extend(personal_images)
        except Exception:
            pass

    if images:
        return images

    _handle_error(ctx, "ConfigError", "No images available", EXIT_CONFIG_ERROR)
    return None


def _resolve_notebook_name(name: Optional[str], *, json_output: bool) -> str:
    if name:
        return name
    generated = f"notebook-{uuid.uuid4().hex[:8]}"
    if not json_output:
        click.echo(f"Generated name: {generated}")
    return generated


def run_notebook_create(
    ctx: Context,
    *,
    name: Optional[str],
    workspace: Optional[str],
    workspace_id: Optional[str],
    resource: str | None,
    project: Optional[str],
    image: Optional[str],
    shm_size: Optional[int],
    auto_stop: bool,
    auto: bool,
    wait: bool,
    post_start: str | None,
    post_start_script: Path | None,
    json_output: bool,
    post_start_mode: str | None = None,
    priority: Optional[int] = None,
    project_explicit: bool = False,
    keepalive: bool | None = None,
    compute_group_name: Optional[str] = None,
    datasets: tuple[str, ...] = (),
    dry_run: bool = False,
) -> None:
    del project_explicit  # Reserved for future behavior; currently inferred from value presence.
    json_output = resolve_json_output(ctx, json_output)

    # Bad reference syntax is the caller's own mistake, so it is answered before a session is read.
    dataset_pairs = parse_dataset_references(ctx, datasets)
    if dataset_pairs is None:
        return

    session = require_web_session(
        ctx,
        hint=(
            "Creating notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )
    config = load_config(ctx)

    try:
        post_start_spec = resolve_notebook_post_start_spec(
            config=config,
            post_start=post_start,
            post_start_script=post_start_script,
            post_start_mode=post_start_mode,
            keepalive=keepalive,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    try:
        resource, project, image, shm_size = _resolve_create_inputs(
            config=config,
            resource=resource,
            project=project,
            image=image,
            shm_size=shm_size,
        )
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    try:
        gpu_count, gpu_pattern, cpu_count = parse_resource_string(resource)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    requested_cpu_count = cpu_count
    resource_display = format_resource_display(gpu_count, gpu_pattern, requested_cpu_count)

    workspace_id = resolve_notebook_workspace_id(
        ctx,
        config=config,
        session=session,
        workspace=workspace,
        workspace_id=workspace_id,
        gpu_count=gpu_count,
        gpu_pattern=gpu_pattern,
    )
    if not workspace_id:
        return

    # The workspace decides whether a dataset mounts, so this waits for the workspace to be settled
    # and runs before anything is scheduled: refusing here costs a lookup, refusing after the create
    # would have cost a half-mounted instance.
    dataset_info: Optional[list[dict[str, str]]] = None
    if dataset_pairs:
        dataset_info = resolve_notebook_dataset_info(
            ctx,
            pairs=dataset_pairs,
            workspace_id=workspace_id,
            session=session,
            json_output=json_output,
        )
        if dataset_info is None:
            return

    task_priority = _resolve_task_priority(priority, config)
    auto_resource_prices_by_group: dict[str, list[dict]] = {}
    # One read of the platform's verdict on which groups can run this notebook,
    # shared by the placement below and by the preview, so the two cannot disagree
    # (#113). What asks for it: a CPU placement, any preview, and any create that
    # names its own group - in the last case the fact decides whether the request is
    # sent at all. A GPU create that picks the group itself is left to its own path,
    # which already selects out of this same offer data.
    capability: Optional[NotebookGroupCapability] = None
    if gpu_count == 0 or dry_run or compute_group_name:
        capability = fetch_notebook_group_capability(workspace_id=workspace_id, session=session)
    compute_group = resolve_notebook_compute_group(
        ctx,
        session=session,
        workspace_id=workspace_id,
        gpu_count=gpu_count,
        gpu_pattern=gpu_pattern,
        requested_cpu_count=requested_cpu_count,
        auto=auto,
        json_output=json_output,
        compute_group_name=compute_group_name,
        task_priority=task_priority,
        resource_prices_by_group=auto_resource_prices_by_group,
        capability=capability,
    )
    if not compute_group:
        return

    (
        logic_compute_group_id,
        selected_gpu_type,
        gpu_pattern,
        resource_display,
        selected_group_name,
    ) = compute_group

    # Facts the resolved group does not convey by itself: quota that exists but
    # cannot run the workload, an override aimed at a group outside the capable
    # set, or offer data that could not be read at all.
    create_warnings = (
        capability.warnings(resolved_group_id=logic_compute_group_id) if capability else []
    )

    schedule = _fetch_notebook_schedule(ctx, workspace_id=workspace_id, session=session)
    if schedule is None:
        return

    if logic_compute_group_id in auto_resource_prices_by_group:
        resource_prices = auto_resource_prices_by_group[logic_compute_group_id]
    else:
        resource_prices = _fetch_resource_prices(
            workspace_id=workspace_id,
            logic_compute_group_id=logic_compute_group_id,
            session=session,
            json_output=json_output,
        )

    quota_selection = resolve_notebook_quota(
        ctx,
        schedule=schedule,
        gpu_count=gpu_count,
        gpu_pattern=gpu_pattern,
        requested_cpu_count=requested_cpu_count,
        selected_gpu_type=selected_gpu_type,
        resource_prices=resource_prices,
    )
    if not quota_selection:
        return
    quota_id, cpu_count, memory_size, selected_gpu_type, resource_display = quota_selection

    resource_spec = resolve_notebook_resource_spec_price(
        ctx,
        resource_prices=resource_prices,
        gpu_count=gpu_count,
        selected_gpu_type=selected_gpu_type,
        gpu_pattern=gpu_pattern,
        logic_compute_group_id=logic_compute_group_id,
        quota_id=quota_id,
        cpu_count=cpu_count,
        memory_size=memory_size,
        requested_cpu_count=requested_cpu_count,
    )
    if not resource_spec:
        return
    resource_spec_price, quota_id, cpu_count, memory_size = resource_spec

    projects = _fetch_workspace_projects(ctx, workspace_id=workspace_id, session=session)
    if projects is None:
        return

    selected_project = resolve_notebook_project(
        ctx,
        projects=projects,
        config=config,
        project=project,
        allow_requested_over_quota=False,
        needs_gpu_quota=(gpu_count > 0),
        json_output=json_output,
        workspace_id=workspace_id,
        session=session,
    )
    if not selected_project:
        return

    # Quota bands first, project cap last: the quota snap may raise a priority into its allowed
    # band, and a cap lifted back above the project maximum is no longer a cap.
    task_priority = _apply_allowed_priority_levels(
        task_priority=task_priority,
        allowed_levels=_find_quota_allowed_priority_levels(
            schedule, group_id=logic_compute_group_id, quota_id=quota_id
        ),
        json_output=json_output,
    )
    task_priority = _cap_task_priority(
        task_priority=task_priority,
        selected_project=selected_project,
        json_output=json_output,
    )

    images = _fetch_notebook_images(
        ctx,
        workspace_id=workspace_id,
        session=session,
        image=image,
        json_output=json_output,
    )
    if images is None:
        return

    selected_image = resolve_notebook_image(
        ctx,
        images=images,
        image=image,
        json_output=json_output,
    )
    if not selected_image:
        return

    if not json_output:
        click.echo(f"Using image: {selected_image.name}")

    name = _resolve_notebook_name(name, json_output=json_output)

    if dry_run:
        preview = _build_create_preview(
            name=name,
            resource_display=resource_display,
            auto_stop=auto_stop,
            shm_size=shm_size,
            task_priority=task_priority,
            workspace_id=workspace_id,
            selected_project=selected_project,
            selected_group_name=selected_group_name,
            logic_compute_group_id=logic_compute_group_id,
            quota_id=quota_id,
            selected_gpu_type=selected_gpu_type,
            gpu_count=gpu_count,
            cpu_count=cpu_count,
            memory_size=memory_size,
            selected_image=selected_image,
            resource_spec_price=resource_spec_price,
            dataset_info=dataset_info,
            warnings=create_warnings,
        )
        if json_output:
            click.echo(json_formatter.format_json(preview))
        else:
            _render_create_preview(preview)
        return

    if not json_output:
        for warning in create_warnings:
            click.echo(f"Warning: {warning}", err=True)

    notebook_id = create_notebook_and_report(
        ctx,
        name=name,
        resource_display=resource_display,
        selected_project=selected_project,
        selected_image=selected_image,
        logic_compute_group_id=logic_compute_group_id,
        compute_group_name=selected_group_name,
        quota_id=quota_id,
        selected_gpu_type=selected_gpu_type,
        gpu_count=gpu_count,
        cpu_count=cpu_count,
        memory_size=memory_size,
        shm_size=shm_size,
        auto_stop=auto_stop,
        workspace_id=workspace_id,
        session=session,
        json_output=json_output,
        task_priority=task_priority,
        resource_spec_price=resource_spec_price,
        dataset_info=dataset_info,
        defer_json_report=wait or post_start_spec is not None,
    )
    if not notebook_id:
        return

    try:
        if not maybe_wait_for_running(
            ctx,
            notebook_id=notebook_id,
            session=session,
            wait=wait,
            needs_post_start=(post_start_spec is not None),
            json_output=json_output,
            timeout=600,
        ):
            return

        maybe_run_post_start(
            ctx,
            notebook_id=notebook_id,
            session=session,
            post_start_spec=post_start_spec,
            gpu_count=gpu_count,
            json_output=json_output,
        )
    except Exception as exc:
        _handle_error(
            ctx,
            "APIError",
            f"Notebook {notebook_id} was created, but the follow-up step failed: {exc}",
            EXIT_API_ERROR,
            hint=f"Check the notebook with 'inspire notebook status {notebook_id}'.",
            data={"notebook_id": notebook_id},
        )
        return

    if json_output and (wait or post_start_spec is not None):
        _emit_create_json_success(
            notebook_id=notebook_id,
            name=name,
            resource_display=resource_display,
            selected_project=selected_project,
            selected_image=selected_image,
            logic_compute_group_id=logic_compute_group_id,
            compute_group_name=selected_group_name,
            workspace_id=workspace_id,
            session=session,
        )
    elif not json_output:
        click.echo(f"\nUse 'inspire notebook status {notebook_id}' to check status.")


__all__ = ["run_notebook_create"]
