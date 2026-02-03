"""Notebook create command."""

from __future__ import annotations

import os
import re
import uuid
from typing import Optional

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.notebook_cli import load_config, require_web_session, resolve_json_output
from inspire.cli.utils.config import ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.workspace import select_workspace_id


def _parse_resource_string(resource: str) -> tuple[int, str, Optional[int]]:
    """Parse a resource string like '1xH200' into (gpu_count, gpu_type, cpu_count).

    Supported formats:
    - "1xH200", "4xH200", "8xH100"
    - "H200", "H100" (defaults to 1 GPU)
    - "1 H200", "4 H100"
    - "4CPU", "4xCPU", "4 CPU" (CPU-only)
    - "CPU" (CPU-only, count resolved from quota)
    - "4x", "4X", "8x" (GPU count only, type auto-selected)
    - "4" (GPU count only, type auto-selected)

    Returns:
        Tuple of (gpu_count, gpu_type_pattern, cpu_count). cpu_count is None
        when the CPU count is unspecified (e.g., "CPU"). gpu_type_pattern is
        "GPU" when the type should be auto-selected.
    """
    resource = resource.strip().upper()

    cpu_aliases = {"CPU", "CPUONLY", "CPU_ONLY", "CPU-ONLY"}

    # Pattern: Nx or NX only (e.g., "4x", "8X") - auto-select GPU type
    match = re.match(r"^(\d+)\s*[xX]$", resource)
    if match:
        count = int(match.group(1))
        return count, "GPU", None  # "GPU" signals auto-select

    # Pattern: N only (e.g., "4", "8") - auto-select GPU type
    match = re.match(r"^(\d+)$", resource)
    if match:
        count = int(match.group(1))
        return count, "GPU", None  # "GPU" signals auto-select

    # Pattern: NxGPU (e.g., "1xH200", "4xH100")
    match = re.match(r"^(\d+)\s*[xX]\s*(\w+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    # Pattern: N GPU (e.g., "1 H200", "4 H100")
    match = re.match(r"^(\d+)\s+(\w+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    # Pattern: NGPU without delimiter (e.g., "4CPU", "4H200")
    match = re.match(r"^(\d+)([A-Z0-9_-]+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    # Pattern: GPU only (e.g., "H200") - defaults to 1
    match = re.match(r"^(\w+)$", resource)
    if match:
        pattern = match.group(1)
        if pattern in cpu_aliases:
            return 0, "CPU", None
        return 1, pattern, None

    raise ValueError(f"Invalid resource format: {resource}")


def _format_resource_display(
    gpu_count: int,
    gpu_pattern: str,
    cpu_count: Optional[int],
) -> str:
    """Format a resource string for display."""
    if gpu_count == 0 and gpu_pattern.upper() == "CPU":
        if cpu_count:
            return f"{cpu_count}xCPU"
        return "CPU"
    return f"{gpu_count}x{gpu_pattern}"


def _match_gpu_type(pattern: str, gpu_type_display: str) -> bool:
    """Check if a GPU type display string matches a pattern.

    Args:
        pattern: User-provided pattern (e.g., "H200", "H100").
        gpu_type_display: GPU type from API (e.g., "H200", "H100-SXM").

    Returns:
        True if matches.
    """
    pattern = pattern.upper()
    gpu_type_display = gpu_type_display.upper()
    return pattern in gpu_type_display


@click.command("create")
@click.option(
    "--name",
    "-n",
    help="Notebook name (auto-generated if omitted)",
)
@click.option(
    "--workspace",
    help="Workspace name (from [workspaces])",
)
@click.option(
    "--workspace-id",
    help="Workspace ID (overrides auto-selection)",
)
@click.option(
    "--resource",
    "-r",
    default=lambda: os.environ.get("INSPIRE_NOTEBOOK_RESOURCE", "1xH200"),
    help="Resource spec (e.g., 1xH200, 4xH100, 4CPU)",
)
@click.option(
    "--project",
    "-p",
    default=lambda: os.environ.get("INSPIRE_PROJECT_ID"),
    help="Project name or ID",
)
@click.option(
    "--image",
    "-i",
    default=lambda: (os.environ.get("INSPIRE_NOTEBOOK_IMAGE") or os.environ.get("INSP_IMAGE")),
    help="Image name/URL (prompts interactively if omitted)",
)
@click.option(
    "--shm-size",
    type=int,
    default=32,
    help="Shared memory size in GB (default: 32)",
)
@click.option(
    "--auto-stop/--no-auto-stop",
    default=False,
    help="Auto-stop when idle",
)
@click.option(
    "--auto/--no-auto",
    default=True,
    help="Auto-select best available compute group based on availability (default: auto)",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for notebook to reach RUNNING status (default: enabled)",
)
@click.option(
    "--keepalive/--no-keepalive",
    default=True,
    help="Run a GPU keepalive script to maintain utilization above 40% (default: enabled)",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def create_notebook_cmd(
    ctx: Context,
    name: Optional[str],
    workspace: Optional[str],
    workspace_id: Optional[str],
    resource: str,
    project: Optional[str],
    image: Optional[str],
    shm_size: int,
    auto_stop: bool,
    auto: bool,
    wait: bool,
    keepalive: bool,
    json_output: bool,
) -> None:
    """Create a new interactive notebook instance.

    \b
    Examples:
        inspire notebook create                     # Interactive mode, auto-select GPU
        inspire notebook create -r 1xH200           # 1 GPU H200
        inspire notebook create -r 4xH100 -n mytest # 4 GPUs H100
        inspire notebook create -r 4x               # 4 GPUs, auto-select type
        inspire notebook create -r 8x               # 8 GPUs (full node), auto-select type
        inspire notebook create -r 4CPU             # 4 CPUs
        inspire notebook create -r 1xH100 --shm-size 64  # With 64GB shared memory
        inspire notebook create --no-auto -r 1xH200 # Disable auto-select
        inspire notebook create --no-keepalive      # Disable GPU keepalive script
        inspire notebook create --no-keepalive --no-wait  # Old behavior (return immediately)
    """
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

    # Parse resource string
    try:
        gpu_count, gpu_pattern, cpu_count = _parse_resource_string(resource)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    requested_cpu_count = cpu_count
    resource_display = _format_resource_display(gpu_count, gpu_pattern, requested_cpu_count)

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

    # Auto-select best compute group based on availability
    auto_selected_group = None
    auto_selected_gpu_type = ""
    if auto and gpu_count > 0:
        # Use auto-select to find the best available compute group
        # gpu_pattern == "GPU" means no specific type was requested
        filter_gpu_type = None if gpu_pattern == "GPU" else gpu_pattern

        try:
            best = browser_api_module.find_best_compute_group_accurate(
                gpu_type=filter_gpu_type,
                min_gpus=gpu_count,
                include_preemptible=True,
                prefer_full_nodes=True,  # Check free nodes for 4x/8x requests
            )

            if best:
                auto_selected_group = best
                # Update gpu_pattern if we auto-selected the GPU type
                if gpu_pattern == "GPU":
                    gpu_pattern = best.gpu_type or "GPU"
                auto_selected_gpu_type = best.gpu_type or ""

                # Show appropriate message based on selection source
                if not json_output:
                    if best.selection_source == "nodes" and best.free_nodes:
                        click.echo(
                            f"Auto-selected: {best.group_name}, "
                            f"{best.free_nodes} full node(s) free ({best.available_gpus} GPUs)"
                        )
                    else:
                        click.echo(
                            f"Auto-selected: {best.group_name}, "
                            f"{best.available_gpus} GPUs available"
                        )
            elif gpu_pattern == "GPU":
                # No availability found and we need to auto-select type
                _handle_error(
                    ctx,
                    "AvailabilityError",
                    f"No compute group has {gpu_count} GPUs available",
                    EXIT_CONFIG_ERROR,
                )
                return
        except Exception as e:
            # Auto-select failed, fall back to manual selection
            if not json_output:
                click.echo(f"Warning: Auto-select failed ({e}), using manual selection", err=True)
            auto_selected_group = None

    # Update resource display after potential auto-selection
    resource_display = _format_resource_display(gpu_count, gpu_pattern, requested_cpu_count)

    if not json_output:
        click.echo(f"Creating notebook with {resource_display}...")

    # 1. Get compute groups and find matching one
    try:
        compute_groups = browser_api_module.list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Error fetching compute groups: {e}", EXIT_API_ERROR)
        return

    # Find compute group with matching resource type
    # If auto-select found a group, use its group_id to find the matching group in notebook compute groups
    selected_group = None
    selected_gpu_type = ""

    if auto_selected_group:
        # Find the notebook compute group that matches the auto-selected group
        for group in compute_groups:
            if group.get("logic_compute_group_id") == auto_selected_group.group_id:
                selected_group = group
                selected_gpu_type = auto_selected_gpu_type
                break
        # If not found by ID, try matching by GPU type
        if not selected_group:
            for group in compute_groups:
                gpu_stats_list = group.get("gpu_type_stats", [])
                for gpu_stats in gpu_stats_list:
                    gpu_info = gpu_stats.get("gpu_info", {})
                    gpu_type_display = gpu_info.get("gpu_type_display", "")
                    if _match_gpu_type(auto_selected_group.gpu_type, gpu_type_display):
                        selected_group = group
                        selected_gpu_type = gpu_info.get("gpu_type", "")
                        break
                if selected_group:
                    break

    # Fall back to manual selection if auto-select didn't work or wasn't used
    if not selected_group:
        for group in compute_groups:
            gpu_stats_list = group.get("gpu_type_stats", [])
            for gpu_stats in gpu_stats_list:
                gpu_info = gpu_stats.get("gpu_info", {})
                gpu_type_display = gpu_info.get("gpu_type_display", "")
                if _match_gpu_type(gpu_pattern, gpu_type_display):
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

    # 2. Get notebook schedule to find quota matching GPU type and count
    try:
        schedule = browser_api_module.get_notebook_schedule(
            workspace_id=workspace_id, session=session
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch notebook schedule: {e}", EXIT_API_ERROR)
        return

    # Parse quota (might be JSON string)
    import json as json_mod

    quota_list = schedule.get("quota", [])
    if isinstance(quota_list, str):
        quota_list = json_mod.loads(quota_list) if quota_list else []

    # Find quota matching GPU/CPU request
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
        resource_display = _format_resource_display(gpu_count, gpu_pattern, cpu_count)

    # 3. Get projects
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

    # Select project
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
        else:  # All projects over quota
            _handle_error(ctx, "QuotaExceeded", error_msg, EXIT_CONFIG_ERROR)
            return

    # 4. Get images
    try:
        images = browser_api_module.list_images(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch images: {e}", EXIT_API_ERROR)
        return

    if not images:
        _handle_error(ctx, "ConfigError", "No images available", EXIT_CONFIG_ERROR)
        return

    # Select image
    selected_image = None
    if image:
        # Match by name, URL, or partial match
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
        # Interactive selection
        if not json_output:
            click.echo("\nAvailable images:")
            for i, img in enumerate(images[:10], 1):  # Show first 10
                click.echo(f"  [{i}] {img.name}")
            if len(images) > 10:
                click.echo(f"  ... and {len(images) - 10} more")

            # Prompt for selection
            default_idx = 1
            # Try to find pytorch as default
            for i, img in enumerate(images, 1):
                if "pytorch" in img.name.lower():
                    default_idx = i
                    break

            try:
                choice = click.prompt(
                    "\nSelect image",
                    type=int,
                    default=default_idx,
                )
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
            # In JSON mode, use first pytorch image or first available
            for img in images:
                if "pytorch" in img.name.lower():
                    selected_image = img
                    break
            if not selected_image:
                selected_image = images[0]

    if not json_output:
        click.echo(f"Using image: {selected_image.name}")

    # 5. Generate name if not provided
    if not name:
        name = f"notebook-{uuid.uuid4().hex[:8]}"
        if not json_output:
            click.echo(f"Generated name: {name}")

    # 6. Create the notebook
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

        # Wait for notebook to be running if requested (or if keepalive is enabled)
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

        # Run keepalive script if requested (GPU notebooks only)
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
                # Don't exit on keepalive failure - the notebook is still usable

        if not json_output:
            click.echo(f"\nUse 'inspire notebook status {notebook_id}' to check status.")

    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to create notebook: {e}", EXIT_API_ERROR)
        return
