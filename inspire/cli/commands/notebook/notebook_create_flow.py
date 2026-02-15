"""Notebook creation flow for `inspire notebook create`."""

from __future__ import annotations

import json
import re
import uuid
from typing import Optional

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import load_config, require_web_session, resolve_json_output
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.session import WebSession


def parse_resource_string(resource: str) -> tuple[int, str, Optional[int]]:
    resource = resource.strip().upper()

    cpu_aliases = {"CPU", "CPUONLY", "CPU_ONLY", "CPU-ONLY"}

    match = re.match(r"^(\d+)\s*[xX]$", resource)
    if match:
        count = int(match.group(1))
        return count, "GPU", None

    match = re.match(r"^(\d+)$", resource)
    if match:
        count = int(match.group(1))
        return count, "GPU", None

    match = re.match(r"^(\d+)\s*[xX]\s*(\w+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    match = re.match(r"^(\d+)\s+(\w+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    match = re.match(r"^(\d+)([A-Z0-9_-]+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    match = re.match(r"^(\w+)$", resource)
    if match:
        pattern = match.group(1)
        if pattern in cpu_aliases:
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
    pattern = pattern.upper()
    gpu_type_display = gpu_type_display.upper()
    return pattern in gpu_type_display


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
        auto_workspace_id = select_workspace_id(
            config,
            gpu_type=gpu_pattern if gpu_count > 0 else None,
            cpu_only=(gpu_count == 0),
            explicit_workspace_id=workspace_id,
            explicit_workspace_name=workspace,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return None

    if not auto_workspace_id:
        auto_workspace_id = session.workspace_id

    if auto_workspace_id == _ZERO_WORKSPACE_ID:
        auto_workspace_id = None

    if not auto_workspace_id:
        hint = (
            "Use --workspace-id, set [workspaces].cpu in config.toml, or set INSPIRE_WORKSPACE_ID."
            if gpu_count == 0
            else "Use --workspace-id, set [workspaces].gpu in config.toml, or set INSPIRE_WORKSPACE_ID."
        )
        _handle_error(
            ctx, "ConfigError", "No workspace_id configured.", EXIT_CONFIG_ERROR, hint=hint
        )
        return None

    return auto_workspace_id


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
) -> tuple[str, str, str, str] | None:
    auto_selected_group = None
    auto_selected_gpu_type = ""

    if auto and gpu_count > 0:
        filter_gpu_type = None if gpu_pattern == "GPU" else gpu_pattern

        try:
            best = browser_api_module.find_best_compute_group_accurate(
                gpu_type=filter_gpu_type,
                min_gpus=gpu_count,
                include_preemptible=True,
                prefer_full_nodes=True,
            )

            if best:
                auto_selected_group = best
                if gpu_pattern == "GPU":
                    gpu_pattern = best.gpu_type or "GPU"
                auto_selected_gpu_type = best.gpu_type or ""

                if not json_output:
                    if best.selection_source == "nodes" and best.free_nodes:
                        click.echo(
                            f"Auto-selected: {best.group_name}, "
                            f"{best.free_nodes} full node(s) free ({best.available_gpus} GPUs)"
                        )
                    else:
                        click.echo(
                            f"Auto-selected: {best.group_name}, {best.available_gpus} GPUs available"
                        )
            elif gpu_pattern == "GPU":
                _handle_error(
                    ctx,
                    "AvailabilityError",
                    f"No compute group has {gpu_count} GPUs available",
                    EXIT_CONFIG_ERROR,
                )
                return None
        except Exception as e:
            if not json_output:
                click.echo(f"Warning: Auto-select failed ({e}), using manual selection", err=True)
            auto_selected_group = None

    resource_display = format_resource_display(gpu_count, gpu_pattern, requested_cpu_count)

    if not json_output:
        click.echo(f"Creating notebook with {resource_display}...")

    try:
        compute_groups = browser_api_module.list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Error fetching compute groups: {e}", EXIT_API_ERROR)
        return None

    selected_group = None
    selected_gpu_type = ""

    if auto_selected_group:
        for group in compute_groups:
            if group.get("logic_compute_group_id") == auto_selected_group.group_id:
                selected_group = group
                selected_gpu_type = auto_selected_gpu_type
                break

        if not selected_group:
            for group in compute_groups:
                gpu_stats_list = group.get("gpu_type_stats", [])
                for gpu_stats in gpu_stats_list:
                    gpu_info = gpu_stats.get("gpu_info", {})
                    gpu_type_display = gpu_info.get("gpu_type_display", "")
                    if match_gpu_type(auto_selected_group.gpu_type, gpu_type_display):
                        selected_group = group
                        selected_gpu_type = gpu_info.get("gpu_type", "")
                        break
                if selected_group:
                    break

    if not selected_group:
        for group in compute_groups:
            gpu_stats_list = group.get("gpu_type_stats", [])
            for gpu_stats in gpu_stats_list:
                gpu_info = gpu_stats.get("gpu_info", {})
                gpu_type_display = gpu_info.get("gpu_type_display", "")
                if match_gpu_type(gpu_pattern, gpu_type_display):
                    selected_group = group
                    selected_gpu_type = gpu_info.get("gpu_type", "")
                    break
            if selected_group:
                break

    if not selected_group and gpu_count == 0:
        for group in compute_groups:
            if not group.get("gpu_type_stats"):
                selected_group = group
                selected_gpu_type = ""
                break

    if not selected_group:
        available_types: set[str] = set()
        for group in compute_groups:
            for stats in group.get("gpu_type_stats", []):
                gpu_type = stats.get("gpu_info", {}).get("gpu_type_display", "Unknown")
                if gpu_type:
                    available_types.add(gpu_type)
        if not available_types and gpu_count == 0:
            available_types.add("CPU")

        hint = None
        if available_types:
            formatted = "\n".join(f"  - {gpu_type}" for gpu_type in sorted(available_types))
            hint = f"Available resource types:\n{formatted}"

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

    return logic_compute_group_id, selected_gpu_type, gpu_pattern, resource_display


def resolve_notebook_quota(
    ctx: Context,
    *,
    schedule: dict,
    gpu_count: int,
    gpu_pattern: str,
    requested_cpu_count: Optional[int],
    selected_gpu_type: str,
) -> tuple[str, int, int, str, str] | None:
    quota_list = schedule.get("quota", [])
    if isinstance(quota_list, str):
        quota_list = json.loads(quota_list) if quota_list else []

    selected_quota = None
    cpu_quotas: list[dict] = []
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
        for quota in quota_list:
            quota_gpu_type = quota.get("gpu_type", "")
            if quota.get("gpu_count") == gpu_count and (
                quota_gpu_type == selected_gpu_type
                or match_gpu_type(selected_gpu_type, quota_gpu_type)
                or match_gpu_type(gpu_pattern, quota_gpu_type)
            ):
                selected_quota = quota
                break

    if not selected_quota:
        # When the schedule API is unavailable, quota_list will be empty.
        # Use reasonable defaults so notebook creation can proceed.
        if not quota_list:
            cpu_count = requested_cpu_count or (20 if gpu_count > 0 else 4)
            memory_size = 200 if gpu_count > 0 else 32
            resource_display = format_resource_display(gpu_count, gpu_pattern, cpu_count)
            return "", cpu_count, memory_size, selected_gpu_type, resource_display

        if gpu_count == 0:
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
            message = f"No quota found for {gpu_count}x {selected_gpu_type}"

            lines = []
            for quota in quota_list:
                quota_name = quota.get("name")
                suffix = f" ({quota_name})" if quota_name else ""
                lines.append(f"  - {quota.get('gpu_count')}x {quota.get('gpu_type')}{suffix}")

            hint = "Available quotas:\n" + "\n".join(lines) if lines else None

        _handle_error(ctx, "ValidationError", message, EXIT_CONFIG_ERROR, hint=hint)
        return None

    quota_id = selected_quota.get("id", "")
    cpu_count = selected_quota.get("cpu_count", 20)
    memory_size = selected_quota.get("memory_size", 200)

    if gpu_count == 0:
        selected_gpu_type = selected_quota.get("gpu_type", "") or ""

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
) -> object | None:
    project_value = project
    if project_value and not project_value.startswith("project-"):
        for alias, project_id in (config.projects or {}).items():
            if alias.lower() == project_value.lower():
                project_value = project_id
                break

    try:
        shared_groups = getattr(config, "project_shared_path_groups", None)
        if not isinstance(shared_groups, dict) or not shared_groups:
            shared_groups = None

        selected_project, fallback_msg = browser_api_module.select_project(
            projects,
            project_value,
            allow_requested_over_quota=allow_requested_over_quota,
            shared_path_group_by_id=shared_groups,
            needs_gpu_quota=needs_gpu_quota,
            project_order=config.project_order or None,
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


def _find_image_match(images: list, image: str) -> object | None:
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
    images: list,
    image: Optional[str],
    json_output: bool,
) -> object | None:
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
) -> tuple[dict, str, int, int]:
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

        for price_entry in resource_prices:
            if price_entry.get("gpu_count", 0) != 0:
                continue

            entry_quota_id = price_entry.get("quota_id", "")
            entry_cpu_count = price_entry.get("cpu_count")

            if quota_id and entry_quota_id and entry_quota_id != quota_id:
                continue
            if requested_cpu_count is not None and entry_cpu_count != requested_cpu_count:
                continue

            entry_cpu_info = price_entry.get("cpu_info", {})
            cpu_type = entry_cpu_info.get("cpu_type", "")
            if cpu_type:
                cpu_spec["cpu_type"] = cpu_type
            if not quota_id and entry_quota_id:
                quota_id = entry_quota_id
                cpu_spec["quota_id"] = entry_quota_id
            break

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
        entry_cpu_info = price_entry.get("cpu_info", {})

        if entry_gpu_count != gpu_count:
            continue
        if not (
            not selected_gpu_type
            or entry_gpu_type == selected_gpu_type
            or match_gpu_type(selected_gpu_type, entry_gpu_type)
            or match_gpu_type(gpu_pattern, entry_gpu_type)
        ):
            continue

        resource_spec_price = {
            "cpu_type": entry_cpu_info.get("cpu_type", ""),
            "cpu_count": price_entry.get("cpu_count", cpu_count),
            "gpu_type": entry_gpu_type,
            "gpu_count": entry_gpu_count,
            "memory_size_gib": price_entry.get("memory_size_gib", memory_size),
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": price_entry.get("quota_id", quota_id),
        }
        if not quota_id:
            quota_id = price_entry.get("quota_id", "")
        cpu_count = price_entry.get("cpu_count", cpu_count)
        mem_gib = price_entry.get("memory_size_gib")
        if mem_gib is not None:
            memory_size = mem_gib
        break

    return resource_spec_price, quota_id, cpu_count, memory_size


def create_notebook_and_report(
    ctx: Context,
    *,
    name: str,
    resource_display: str,
    selected_project,
    selected_image,
    logic_compute_group_id: str,
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
) -> str | None:
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
        )

        notebook_id = result.get("notebook_id", "")

        if json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "notebook_id": notebook_id,
                        "name": name,
                        "resource": resource_display,
                        "project": selected_project.name,
                        "image": selected_image.name,
                    }
                )
            )
        else:
            click.echo("\nNotebook created successfully!")
            click.echo(f"  ID: {notebook_id}")
            click.echo(f"  Name: {name}")
            click.echo(f"  Resource: {resource_display}")

        return notebook_id

    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to create notebook: {e}", EXIT_API_ERROR)
        return None


def maybe_wait_for_running(
    ctx: Context,
    *,
    notebook_id: str,
    session: WebSession,
    wait: bool,
    keepalive: bool,
    json_output: bool,
    timeout: int = 600,
) -> bool:
    if not (wait or keepalive):
        return True

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
        _handle_error(ctx, "NotebookFailed", msg, EXIT_API_ERROR, hint="\n".join(hint_parts))
        return False
    except TimeoutError as e:
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook to reach RUNNING: {e}",
            EXIT_API_ERROR,
        )
        return False


def maybe_start_keepalive(
    ctx: Context,
    *,
    notebook_id: str,
    session: WebSession,
    keepalive: bool,
    gpu_count: int,
    json_output: bool,
) -> None:
    if not (keepalive and gpu_count > 0):
        return

    from inspire.cli.utils.keepalive import get_keepalive_command

    if not json_output:
        click.echo("Starting GPU keepalive script...")

    try:
        browser_api_module.run_command_in_notebook(
            notebook_id=notebook_id,
            command=get_keepalive_command(),
            session=session,
        )
        if not json_output:
            click.echo("GPU keepalive script started (log: /tmp/keepalive.log)")
            click.echo('  To stop: inspire bridge exec "kill $(cat /tmp/keepalive.pid)"')
    except Exception as e:
        if not json_output:
            click.echo(f"Warning: Failed to start keepalive script: {e}", err=True)


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
    keepalive: bool,
    json_output: bool,
    priority: Optional[int] = None,
    project_explicit: bool = False,
) -> None:
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Creating notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )
    config = load_config(ctx)

    if not resource:
        resource = config.notebook_resource
    if not project:
        project = config.job_project_id
    if not image:
        image = config.notebook_image or config.job_image
    if shm_size is None:
        shm_size = config.shm_size if config.shm_size is not None else 32
    if shm_size < 1:
        _handle_error(ctx, "ValidationError", "Shared memory size must be >= 1.", EXIT_CONFIG_ERROR)
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

    compute_group = resolve_notebook_compute_group(
        ctx,
        session=session,
        workspace_id=workspace_id,
        gpu_count=gpu_count,
        gpu_pattern=gpu_pattern,
        requested_cpu_count=requested_cpu_count,
        auto=auto,
        json_output=json_output,
    )
    if not compute_group:
        return

    logic_compute_group_id, selected_gpu_type, gpu_pattern, resource_display = compute_group

    try:
        schedule = browser_api_module.get_notebook_schedule(
            workspace_id=workspace_id, session=session
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch notebook schedule: {e}", EXIT_API_ERROR)
        return

    quota_selection = resolve_notebook_quota(
        ctx,
        schedule=schedule,
        gpu_count=gpu_count,
        gpu_pattern=gpu_pattern,
        requested_cpu_count=requested_cpu_count,
        selected_gpu_type=selected_gpu_type,
    )
    if not quota_selection:
        return

    quota_id, cpu_count, memory_size, selected_gpu_type, resource_display = quota_selection

    # --- Fetch resource prices (needed for resource_spec_price in create body) ---
    resource_prices: list[dict] = []
    if logic_compute_group_id:
        try:
            resource_prices = browser_api_module.get_resource_prices(
                workspace_id=workspace_id,
                logic_compute_group_id=logic_compute_group_id,
                session=session,
            )
        except Exception as e:
            if not json_output:
                click.echo(f"Warning: Failed to fetch resource prices: {e}", err=True)

    # Build resource_spec_price matching create API expectations.
    # For CPU, keep the selected quota/requested size as source of truth.
    # For GPU, prefer resource_prices entries when available.
    resource_spec_price, quota_id, cpu_count, memory_size = resolve_notebook_resource_spec_price(
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

    # --- Resolve task priority ---
    task_priority = priority
    if task_priority is None:
        task_priority = config.job_priority if hasattr(config, "job_priority") else None

    try:
        projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch projects: {e}", EXIT_API_ERROR)
        return

    if not projects:
        _handle_error(
            ctx, "ConfigError", "No projects available in this workspace", EXIT_CONFIG_ERROR
        )
        return

    selected_project = resolve_notebook_project(
        ctx,
        projects=projects,
        config=config,
        project=project,
        allow_requested_over_quota=False,
        needs_gpu_quota=(gpu_count > 0),
        json_output=json_output,
    )
    if not selected_project:
        return

    # Cap task priority to the selected project's max priority
    if selected_project.priority_name:
        try:
            max_priority = int(selected_project.priority_name)
            if task_priority is not None and task_priority > max_priority:
                if not json_output:
                    click.echo(
                        f"Capping priority {task_priority} → {max_priority} "
                        f"(max for project '{selected_project.name}')"
                    )
                task_priority = max_priority
        except ValueError:
            pass

    try:
        images = browser_api_module.list_images(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch images: {e}", EXIT_API_ERROR)
        return

    # When a specific image is requested and not found in official images,
    # also search public images before giving up.
    if image and not _find_image_match(images, image):
        try:
            public_images = browser_api_module.list_images(
                workspace_id=workspace_id, source="SOURCE_PUBLIC", session=session
            )
            if public_images:
                if not json_output:
                    click.echo("Searching public images...")
                images = images + public_images
        except Exception:
            pass

    if not images:
        _handle_error(ctx, "ConfigError", "No images available", EXIT_CONFIG_ERROR)
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

    if not name:
        name = f"notebook-{uuid.uuid4().hex[:8]}"
        if not json_output:
            click.echo(f"Generated name: {name}")

    notebook_id = create_notebook_and_report(
        ctx,
        name=name,
        resource_display=resource_display,
        selected_project=selected_project,
        selected_image=selected_image,
        logic_compute_group_id=logic_compute_group_id,
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
    )
    if not notebook_id:
        return

    if not maybe_wait_for_running(
        ctx,
        notebook_id=notebook_id,
        session=session,
        wait=wait,
        keepalive=keepalive,
        json_output=json_output,
        timeout=600,
    ):
        return

    maybe_start_keepalive(
        ctx,
        notebook_id=notebook_id,
        session=session,
        keepalive=keepalive,
        gpu_count=gpu_count,
        json_output=json_output,
    )

    if not json_output:
        click.echo(f"\nUse 'inspire notebook status {notebook_id}' to check status.")


__all__ = ["run_notebook_create"]
