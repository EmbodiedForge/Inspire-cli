"""Notebook creation flow (implementation for `inspire notebook create`)."""

from __future__ import annotations

import uuid
from typing import Optional

import click

from inspire.cli.commands.notebook_create_helpers import (
    format_resource_display,
    match_gpu_type,
    parse_resource_string,
)
from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.config import ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import load_config, require_web_session, resolve_json_output
from inspire.cli.utils.workspace import select_workspace_id


def run_notebook_create(
    ctx: Context,
    *,
    name: Optional[str],
    workspace: Optional[str],
    workspace_id: Optional[str],
    resource: str,
    project: Optional[str],
    image: Optional[str],
    shm_size: Optional[int],
    auto_stop: bool,
    auto: bool,
    wait: bool,
    keepalive: bool,
    json_output: bool,
) -> None:
    """Run the notebook creation flow."""
    from inspire.cli.utils.keepalive import get_keepalive_command

    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Creating notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )
    config = load_config(ctx)
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
        return

    if not auto_workspace_id:
        auto_workspace_id = session.workspace_id

    if auto_workspace_id == "ws-00000000-0000-0000-0000-000000000000":
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
        return

    workspace_id = auto_workspace_id

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
                return
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
        return

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
        return

    logic_compute_group_id = selected_group.get("logic_compute_group_id")

    try:
        schedule = browser_api_module.get_notebook_schedule(
            workspace_id=workspace_id, session=session
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch notebook schedule: {e}", EXIT_API_ERROR)
        return

    import json as json_mod

    quota_list = schedule.get("quota", [])
    if isinstance(quota_list, str):
        quota_list = json_mod.loads(quota_list) if quota_list else []

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
            if quota.get("gpu_type") == selected_gpu_type and quota.get("gpu_count") == gpu_count:
                selected_quota = quota
                break

    if not selected_quota:
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
        return

    quota_id = selected_quota.get("id", "")
    cpu_count = selected_quota.get("cpu_count", 20)
    memory_size = selected_quota.get("memory_size", 200)
    if gpu_count == 0:
        selected_gpu_type = selected_quota.get("gpu_type", "") or ""
        resource_display = format_resource_display(gpu_count, gpu_pattern, cpu_count)

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

    try:
        selected_project, fallback_msg = browser_api_module.select_project(projects, project)

        if not json_output:
            if fallback_msg:
                click.echo(fallback_msg)
            click.echo(
                f"Using project: {selected_project.name}{selected_project.get_quota_status()}"
            )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            hint = None
            if projects:
                hint = "Available projects:\n" + "\n".join(f"  - {p.name}" for p in projects)
            _handle_error(ctx, "ValidationError", error_msg, EXIT_CONFIG_ERROR, hint=hint)
            return
        _handle_error(ctx, "QuotaExceeded", error_msg, EXIT_CONFIG_ERROR)
        return

    try:
        images = browser_api_module.list_images(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch images: {e}", EXIT_API_ERROR)
        return

    if not images:
        _handle_error(ctx, "ConfigError", "No images available", EXIT_CONFIG_ERROR)
        return

    selected_image = None
    if image:
        image_lower = image.lower()
        for img in images:
            if (
                image_lower in img.name.lower()
                or image_lower in img.url.lower()
                or img.image_id == image
            ):
                selected_image = img
                break
        if not selected_image:
            hint = "Available images:\n" + "\n".join(f"  - {img.name}" for img in images[:20])
            _handle_error(
                ctx,
                "ValidationError",
                f"Image '{image}' not found",
                EXIT_CONFIG_ERROR,
                hint=hint,
            )
            return
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
                    return
                selected_image = images[choice - 1]
            except click.Abort:
                _handle_error(ctx, "Aborted", "Aborted.", EXIT_CONFIG_ERROR)
                return
        else:
            for img in images:
                if "pytorch" in img.name.lower():
                    selected_image = img
                    break
            if not selected_image:
                selected_image = images[0]

    if not json_output:
        click.echo(f"Using image: {selected_image.name}")

    if not name:
        name = f"notebook-{uuid.uuid4().hex[:8]}"
        if not json_output:
            click.echo(f"Generated name: {name}")

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

        if wait or keepalive:
            if not json_output:
                click.echo("Waiting for notebook to reach RUNNING status...")
            try:
                browser_api_module.wait_for_notebook_running(
                    notebook_id=notebook_id, session=session, timeout=600
                )
                if not json_output:
                    click.echo("Notebook is now RUNNING.")
            except TimeoutError as e:
                _handle_error(
                    ctx,
                    "Timeout",
                    f"Timed out waiting for notebook to reach RUNNING: {e}",
                    EXIT_API_ERROR,
                )
                return

        if keepalive and gpu_count > 0:
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
            except Exception as e:
                if not json_output:
                    click.echo(f"Warning: Failed to start keepalive script: {e}", err=True)

        if not json_output:
            click.echo(f"\nUse 'inspire notebook status {notebook_id}' to check status.")

    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to create notebook: {e}", EXIT_API_ERROR)
        return
