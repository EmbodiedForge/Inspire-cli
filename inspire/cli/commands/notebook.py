"""Notebook/Interactive instance commands.

Usage:
    inspire notebook list
    inspire notebook status <instance-id>
    inspire notebook create --resource 1xH200
    inspire notebook stop <instance-id>
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

import click
import requests

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_API_ERROR,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.web_session import get_web_session


def _get_base_url() -> str:
    return os.environ.get("INSPIRE_BASE_URL", "https://api.example.com")


def _resolve_json_output(ctx: Context, json_output: bool) -> bool:
    if json_output and not ctx.json_output:
        ctx.json_output = True
    return ctx.json_output


@click.group()
def notebook():
    """Manage notebook/interactive instances.

    \b
    Examples:
        inspire notebook list              # List all instances
        inspire notebook list --json       # List as JSON
    """
    pass


@notebook.command("list")
@click.option(
    "--workspace-id",
    help="Workspace ID (defaults to configured workspace)",
)
@click.option(
    "--all", "-a",
    "show_all",
    is_flag=True,
    help="Show all notebooks (not just your own)",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def list_notebooks(
    ctx: Context,
    workspace_id: Optional[str],
    show_all: bool,
    json_output: bool,
) -> None:
    """List notebook/interactive instances.

    \b
    Examples:
        inspire notebook list
        inspire notebook list --all
        inspire notebook list --workspace-id ws-xxx
        inspire notebook list --json
    """
    from inspire.cli.utils.web_session import get_web_session, request_json

    json_output = _resolve_json_output(ctx, json_output)

    # Get web session for authentication
    try:
        session = get_web_session()
    except ValueError as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    str(e),
                    EXIT_CONFIG_ERROR,
                    hint="Set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables.",
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
            click.echo(
                "\nNote: Listing notebooks requires web authentication. "
                "Please set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables.",
                err=True,
            )
        return sys.exit(EXIT_CONFIG_ERROR)

    # Use workspace_id from session if not provided
    if not workspace_id:
        workspace_id = session.workspace_id
        if not workspace_id:
            if json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "ConfigError",
                        "No workspace_id configured or provided.",
                        EXIT_CONFIG_ERROR,
                        hint="Use --workspace-id or set INSPIRE_WORKSPACE_ID environment variable.",
                    ),
                    err=True,
                )
            else:
                click.echo(
                    "Error: No workspace_id configured or provided. "
                    "Use --workspace-id or set INSPIRE_WORKSPACE_ID environment variable.",
                    err=True,
                )
            return sys.exit(EXIT_CONFIG_ERROR)

    base_url = _get_base_url()

    # Get current user ID for filtering (unless --all is specified)
    user_ids: list[str] = []
    if not show_all:
        try:
            user_data = request_json(
                session,
                "GET",
                f"{base_url}/api/v1/user/detail",
                timeout=30,
            )
            user_id = user_data.get("data", {}).get("id")
            if user_id:
                user_ids = [user_id]
        except Exception:
            pass  # Fall back to showing all if we can't get user ID

    # Use POST with structured body (matches web UI API format)
    body = {
        "workspace_id": workspace_id,
        "page": 1,
        "page_size": 100,
        "filter_by": {
            "keyword": "",
            "user_id": user_ids,
            "logic_compute_group_id": [],
            "status": [],
            "mirror_url": [],
        },
        "order_by": [{"field": "created_at", "order": "desc"}],
    }

    try:
        data = request_json(
            session,
            "POST",
            f"{base_url}/api/v1/notebook/list",
            body=body,
            timeout=30,
        )

        if data.get("code") != 0:
            message = data.get("message", "Unknown error")
            if json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "APIError",
                        f"API error: {message}",
                        EXIT_API_ERROR,
                    ),
                    err=True,
                )
            else:
                click.echo(f"Error: {message}", err=True)
            return sys.exit(EXIT_API_ERROR)

        # API returns items in data.list (not data.items)
        items = data.get("data", {}).get("list", [])
        _print_notebook_list(items, json_output, ctx)

    except ValueError as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "APIError",
                    str(e),
                    EXIT_API_ERROR,
                    hint="Check auth and proxy configuration.",
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        return sys.exit(EXIT_API_ERROR)


def _print_notebook_list(items: list, json_output: bool, ctx: Context) -> None:
    """Print notebook list in appropriate format."""
    if json_output:
        click.echo(json_formatter.format_json({"items": items, "total": len(items)}))
    else:
        if not items:
            click.echo("No notebook instances found.")
            return

        # Table header
        lines = [
            f"{'Name':<25} {'Status':<12} {'Resource':<12} {'ID':<38}",
            "-" * 90,
        ]

        for item in items:
            name = item.get("name", "N/A")[:25]
            status = item.get("status", "Unknown")[:12]
            notebook_id = item.get("notebook_id", item.get("id", "N/A"))

            # Try to get GPU info from quota or resource_spec_price
            resource_info = "N/A"
            quota = item.get("quota") or {}
            gpu_count = quota.get("gpu_count", 0)

            if gpu_count and gpu_count > 0:
                # Get GPU type from resource_spec_price
                gpu_info = (item.get("resource_spec_price") or {}).get("gpu_info") or {}
                gpu_type = gpu_info.get("gpu_product_simple", "GPU")
                resource_info = f"{gpu_count}x{gpu_type}"
            else:
                cpu_count = quota.get("cpu_count", 0)
                if cpu_count:
                    resource_info = f"{cpu_count}xCPU"

            lines.append(f"{name:<25} {status:<12} {resource_info:<12} {notebook_id:<38}")

        click.echo("\n".join(lines))


@notebook.command("status")
@click.argument("instance_id")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def notebook_status(
    ctx: Context,
    instance_id: str,
    json_output: bool,
) -> None:
    """Get status of a notebook instance.

    \b
    Examples:
        inspire notebook status notebook-abc-123
    """
    from inspire.cli.utils.web_session import get_web_session, request_json

    json_output = _resolve_json_output(ctx, json_output)

    try:
        session = get_web_session()
    except ValueError as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    str(e),
                    EXIT_CONFIG_ERROR,
                    hint="Set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables.",
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        return sys.exit(EXIT_CONFIG_ERROR)

    base_url = _get_base_url()

    try:
        data = request_json(
            session,
            "GET",
            f"{base_url}/api/v1/notebook/{instance_id}",
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except ValueError as e:
        message = str(e)
        if "API returned 404" in message:
            if json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "NotFound",
                        f"Notebook instance '{instance_id}' not found",
                        EXIT_API_ERROR,
                    ),
                    err=True,
                )
            else:
                click.echo(
                    f"Error: Notebook instance '{instance_id}' not found",
                    err=True,
                )
        else:
            if json_output:
                click.echo(
                    json_formatter.format_json_error("APIError", message, EXIT_API_ERROR),
                    err=True,
                )
            else:
                click.echo(f"Error: {message}", err=True)
        return sys.exit(EXIT_API_ERROR)
    except Exception as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error("APIError", str(e), EXIT_API_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        return sys.exit(EXIT_API_ERROR)

    if data.get("code") == 0:
        notebook = data.get("data", {})
        if json_output:
            click.echo(json_formatter.format_json(notebook))
        else:
            _print_notebook_detail(notebook)
    else:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "APIError",
                    data.get("message", "Unknown error"),
                    EXIT_API_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {data.get('message', 'Unknown error')}", err=True)
        return sys.exit(EXIT_API_ERROR)


def _print_notebook_detail(notebook: dict) -> None:
    """Print detailed notebook information."""
    click.echo(f"\n{'='*60}")
    click.echo(f"Notebook: {notebook.get('name', 'N/A')}")
    click.echo(f"{'='*60}")

    fields = [
        ("ID", notebook.get("id")),
        ("Status", notebook.get("status")),
        ("Project", notebook.get("project_name")),
        ("Created", notebook.get("created_at")),
    ]

    # Resource spec
    if "resource_spec" in notebook:
        spec = notebook["resource_spec"]
        fields.extend([
            ("GPU Count", spec.get("gpu_count")),
            ("GPU Type", spec.get("gpu_type")),
            ("CPU", spec.get("cpu_count")),
            ("Memory", spec.get("memory_size")),
        ])

    for label, value in fields:
        if value:
            click.echo(f"  {label:<15}: {value}")

    click.echo(f"{'='*60}\n")


def _parse_resource_string(resource: str) -> tuple[int, str, Optional[int]]:
    """Parse a resource string like '1xH200' into (gpu_count, gpu_type, cpu_count).

    Supported formats:
    - "1xH200", "4xH200", "8xH100"
    - "H200", "H100" (defaults to 1 GPU)
    - "1 H200", "4 H100"
    - "4CPU", "4xCPU", "4 CPU" (CPU-only)
    - "CPU" (CPU-only, count resolved from quota)

    Returns:
        Tuple of (gpu_count, gpu_type_pattern, cpu_count). cpu_count is None
        when the CPU count is unspecified (e.g., "CPU").
    """
    resource = resource.strip().upper()

    cpu_aliases = {"CPU", "CPUONLY", "CPU_ONLY", "CPU-ONLY"}

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


def _load_ssh_public_key(pubkey_path: Optional[str] = None) -> str:
    """Load an SSH public key to authorize notebook SSH access."""
    candidates: list[Path]

    if pubkey_path:
        candidates = [Path(pubkey_path).expanduser()]
    else:
        candidates = [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]

    for path in candidates:
        if path.exists():
            key = path.read_text(encoding="utf-8", errors="ignore").strip()
            if key:
                return key

    raise ValueError(
        "No SSH public key found. Provide --pubkey PATH or generate one with 'ssh-keygen'."
    )


@notebook.command("create")
@click.option(
    "--name", "-n",
    help="Notebook name (auto-generated if omitted)",
)
@click.option(
    "--resource", "-r",
    default=lambda: os.environ.get("INSPIRE_NOTEBOOK_RESOURCE", "1xH200"),
    help="Resource spec (e.g., 1xH200, 4xH100, 4CPU)",
)
@click.option(
    "--project", "-p",
    default=lambda: os.environ.get("INSPIRE_PROJECT_ID"),
    help="Project name or ID",
)
@click.option(
    "--image", "-i",
    default=lambda: (
        os.environ.get("INSPIRE_NOTEBOOK_IMAGE")
        or os.environ.get("INSP_IMAGE")
    ),
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
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def create_notebook_cmd(
    ctx: Context,
    name: Optional[str],
    resource: str,
    project: Optional[str],
    image: Optional[str],
    shm_size: int,
    auto_stop: bool,
    json_output: bool,
) -> None:
    """Create a new interactive notebook instance.

    \b
    Examples:
        inspire notebook create                     # Interactive mode
        inspire notebook create -r 1xH200           # 1 GPU H200
        inspire notebook create -r 4xH100 -n mytest # 4 GPUs H100
        inspire notebook create -r 4CPU             # 4 CPUs
        inspire notebook create -r 1xH100 --shm-size 64  # With 64GB shared memory
    """
    from inspire.cli.utils.web_session import get_web_session
    from inspire.cli.utils.browser_api import (
        list_projects,
        list_images,
        list_notebook_compute_groups,
        get_notebook_schedule,
        create_notebook,
    )

    json_output = _resolve_json_output(ctx, json_output)

    # Get web session for authentication
    try:
        session = get_web_session()
    except ValueError as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    str(e),
                    EXIT_CONFIG_ERROR,
                    hint="Set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables.",
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
            click.echo(
                "\nNote: Creating notebooks requires web authentication. "
                "Please set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables.",
                err=True,
            )
        sys.exit(EXIT_CONFIG_ERROR)
        return

    workspace_id = session.workspace_id
    if not workspace_id:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    "No workspace_id configured.",
                    EXIT_CONFIG_ERROR,
                    hint="Set INSPIRE_WORKSPACE_ID environment variable.",
                ),
                err=True,
            )
        else:
            click.echo(
                "Error: No workspace_id configured. "
                "Set INSPIRE_WORKSPACE_ID environment variable.",
                err=True,
            )
        sys.exit(EXIT_CONFIG_ERROR)
        return

    # Parse resource string
    try:
        gpu_count, gpu_pattern, cpu_count = _parse_resource_string(resource)
    except ValueError as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ValidationError",
                    str(e),
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    requested_cpu_count = cpu_count
    resource_display = _format_resource_display(gpu_count, gpu_pattern, requested_cpu_count)

    if not json_output:
        click.echo(f"Creating notebook with {resource_display}...")

    # 1. Get compute groups and find matching one
    try:
        compute_groups = list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )
    except Exception as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "APIError",
                    f"Error fetching compute groups: {e}",
                    EXIT_API_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(f"Error fetching compute groups: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return

    # Find compute group with matching resource type
    selected_group = None
    selected_gpu_type = ""
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
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ValidationError",
                    f"No compute group found with resource type matching '{gpu_pattern}'",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
            sys.exit(EXIT_CONFIG_ERROR)
            return

        click.echo(
            f"Error: No compute group found with resource type matching '{gpu_pattern}'",
            err=True,
        )
        click.echo("\nAvailable resource types:", err=True)
        available = set()
        for group in compute_groups:
            for stats in group.get("gpu_type_stats", []):
                gpu_type = stats.get("gpu_info", {}).get("gpu_type_display", "Unknown")
                if gpu_type:
                    available.add(gpu_type)
        if available:
            for gpu_type in sorted(available):
                click.echo(f"  - {gpu_type}", err=True)
        elif gpu_count == 0:
            click.echo("  - CPU", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    logic_compute_group_id = selected_group.get("logic_compute_group_id")

    # 2. Get notebook schedule to find quota matching GPU type and count
    try:
        schedule = get_notebook_schedule(workspace_id=workspace_id, session=session)
    except Exception as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "APIError",
                    f"Error fetching notebook schedule: {e}",
                    EXIT_API_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(f"Error fetching notebook schedule: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
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
        if json_output:
            if gpu_count == 0:
                requested_label = (
                    f"{requested_cpu_count}xCPU" if requested_cpu_count is not None else "CPU"
                )
                message = f"No quota found for {requested_label}"
            else:
                message = f"No quota found for {gpu_count}x {selected_gpu_type}"
            click.echo(
                json_formatter.format_json_error(
                    "ValidationError",
                    message,
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
            sys.exit(EXIT_CONFIG_ERROR)
            return

        if gpu_count == 0:
            requested_label = (
                f"{requested_cpu_count}xCPU" if requested_cpu_count is not None else "CPU"
            )
            click.echo(f"Error: No quota found for {requested_label}", err=True)
            click.echo("\nAvailable CPU quotas:", err=True)
            for quota in cpu_quotas:
                quota_cpu = quota.get("cpu_count")
                quota_name = quota.get("name")
                label = f"{quota_cpu}xCPU" if quota_cpu else "CPU"
                if quota_name:
                    click.echo(f"  - {label} ({quota_name})", err=True)
                else:
                    click.echo(f"  - {label}", err=True)
        else:
            click.echo(f"Error: No quota found for {gpu_count}x {selected_gpu_type}", err=True)
            click.echo("\nAvailable quotas:", err=True)
            for q in quota_list:
                click.echo(f"  - {q.get('gpu_count')}x {q.get('gpu_type')} ({q.get('name')})", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    quota_id = selected_quota.get("id", "")
    cpu_count = selected_quota.get("cpu_count", 20)
    memory_size = selected_quota.get("memory_size", 200)
    if gpu_count == 0:
        selected_gpu_type = selected_quota.get("gpu_type", "") or ""
        resource_display = _format_resource_display(gpu_count, gpu_pattern, cpu_count)

    # 3. Get projects
    try:
        projects = list_projects(workspace_id=workspace_id, session=session)
    except Exception as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "APIError",
                    f"Error fetching projects: {e}",
                    EXIT_API_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(f"Error fetching projects: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return

    if not projects:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    "No projects available in this workspace",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo("Error: No projects available in this workspace", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    # Select project
    selected_project = None
    if project:
        # Match by name or ID
        for p in projects:
            if p.name.lower() == project.lower() or p.project_id == project:
                selected_project = p
                break
        if not selected_project:
            if json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "ValidationError",
                        f"Project '{project}' not found",
                        EXIT_CONFIG_ERROR,
                    ),
                    err=True,
                )
                sys.exit(EXIT_CONFIG_ERROR)
                return

            click.echo(f"Error: Project '{project}' not found", err=True)
            click.echo("\nAvailable projects:", err=True)
            for p in projects:
                click.echo(f"  - {p.name}", err=True)
            sys.exit(EXIT_CONFIG_ERROR)
            return
    else:
        # Use first project as default
        selected_project = projects[0]
        if not json_output:
            click.echo(f"Using project: {selected_project.name}")

    # 4. Get images
    try:
        images = list_images(workspace_id=workspace_id, session=session)
    except Exception as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "APIError",
                    f"Error fetching images: {e}",
                    EXIT_API_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(f"Error fetching images: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return

    if not images:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    "No images available",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo("Error: No images available", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    # Select image
    selected_image = None
    if image:
        # Match by name, URL, or partial match
        image_lower = image.lower()
        for img in images:
            if (image_lower in img.name.lower() or
                image_lower in img.url.lower() or
                img.image_id == image):
                selected_image = img
                break
        if not selected_image:
            if json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "ValidationError",
                        f"Image '{image}' not found",
                        EXIT_CONFIG_ERROR,
                    ),
                    err=True,
                )
            else:
                click.echo(f"Error: Image '{image}' not found", err=True)
            sys.exit(EXIT_CONFIG_ERROR)
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
                    click.echo("Invalid selection", err=True)
                    sys.exit(EXIT_CONFIG_ERROR)
                    return
                selected_image = images[choice - 1]
            except click.Abort:
                click.echo("\nAborted.", err=True)
                sys.exit(EXIT_CONFIG_ERROR)
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
        result = create_notebook(
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
            click.echo(f"\nNotebook created successfully!")
            click.echo(f"  ID: {notebook_id}")
            click.echo(f"  Name: {name}")
            click.echo(f"  Resource: {resource_display}")
            click.echo(f"\nUse 'inspire notebook status {notebook_id}' to check status.")

    except Exception as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error("APIError", str(e), EXIT_API_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error creating notebook: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return


@notebook.command("stop")
@click.argument("notebook_id")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def stop_notebook_cmd(
    ctx: Context,
    notebook_id: str,
    json_output: bool,
) -> None:
    """Stop a running notebook instance.

    \b
    Examples:
        inspire notebook stop abc123-def456
    """
    from inspire.cli.utils.web_session import get_web_session
    from inspire.cli.utils.browser_api import stop_notebook

    json_output = _resolve_json_output(ctx, json_output)

    try:
        session = get_web_session()
    except ValueError as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    str(e),
                    EXIT_CONFIG_ERROR,
                    hint="Set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables.",
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    try:
        result = stop_notebook(notebook_id=notebook_id, session=session)

        if json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "notebook_id": notebook_id,
                        "status": "stopping",
                        "result": result,
                    }
                )
            )
        else:
            click.echo(f"Notebook '{notebook_id}' is being stopped.")
            click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")

    except Exception as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error("APIError", str(e), EXIT_API_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error stopping notebook: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return


@notebook.command("start")
@click.argument("notebook_id")
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for notebook to reach RUNNING status",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def start_notebook_cmd(
    ctx: Context,
    notebook_id: str,
    wait: bool,
    json_output: bool,
) -> None:
    """Start a stopped notebook instance.

    \b
    Examples:
        inspire notebook start abc123-def456
        inspire notebook start abc123-def456 --wait
    """
    from inspire.cli.utils.web_session import get_web_session
    from inspire.cli.utils.browser_api import start_notebook, wait_for_notebook_running

    json_output = _resolve_json_output(ctx, json_output)

    try:
        session = get_web_session()
    except ValueError as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    str(e),
                    EXIT_CONFIG_ERROR,
                    hint="Set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables.",
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    try:
        result = start_notebook(notebook_id=notebook_id, session=session)

        if not json_output:
            click.echo(f"Notebook '{notebook_id}' is being started.")

        if wait:
            if not json_output:
                click.echo("Waiting for notebook to reach RUNNING status...")
            try:
                wait_for_notebook_running(notebook_id=notebook_id, session=session)
                if not json_output:
                    click.echo("Notebook is now RUNNING.")
            except TimeoutError as e:
                if json_output:
                    click.echo(
                        json_formatter.format_json_error("Timeout", str(e), EXIT_API_ERROR),
                        err=True,
                    )
                else:
                    click.echo(f"Timeout: {e}", err=True)
                sys.exit(EXIT_API_ERROR)
                return

        if json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "notebook_id": notebook_id,
                        "status": "starting",
                        "result": result,
                    }
                )
            )
        else:
            click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")

    except Exception as e:
        if json_output:
            click.echo(
                json_formatter.format_json_error("APIError", str(e), EXIT_API_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error starting notebook: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return


@notebook.command("ssh")
@click.argument("notebook_id")
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for notebook to reach RUNNING status",
)
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="SSH public key path to authorize (defaults to ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub)",
)
@click.option(
    "--save-as",
    help="Save this notebook tunnel as a named profile (usable with 'ssh <name>' after 'inspire tunnel ssh-config --install')",
)
@click.option(
    "--port",
    default=31337,
    show_default=True,
    help="rtunnel server listen port inside notebook",
)
@click.option(
    "--ssh-port",
    default=22222,
    show_default=True,
    help="sshd port inside notebook",
)
@click.option(
    "--command",
    help="Optional remote command to run (if omitted, opens an interactive shell)",
)
@click.option(
    "--rtunnel-bin",
    help="Path to pre-cached rtunnel binary (e.g., /inspire/.../rtunnel)",
)
@click.option(
    "--debug-playwright",
    is_flag=True,
    help="Run browser automation with visible window for debugging",
)
@click.option(
    "--timeout",
    "setup_timeout",
    default=300,
    show_default=True,
    help="Timeout in seconds for rtunnel setup to complete",
)
@pass_context
def ssh_notebook_cmd(
    ctx: Context,
    notebook_id: str,
    wait: bool,
    pubkey: Optional[str],
    save_as: Optional[str],
    port: int,
    ssh_port: int,
    command: Optional[str],
    rtunnel_bin: Optional[str],
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    """SSH into a running notebook instance via rtunnel ProxyCommand."""

    from inspire.cli.utils.web_session import get_web_session
    from inspire.cli.utils.browser_api import (
        get_notebook_detail,
        wait_for_notebook_running,
        setup_notebook_rtunnel,
    )
    from inspire.cli.utils.tunnel import (
        BridgeProfile,
        TunnelConfig,
        get_ssh_command_args,
        load_tunnel_config,
        save_tunnel_config,
    )

    try:
        session = get_web_session()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        click.echo(
            "\nNote: Notebook SSH requires web authentication. "
            "Please set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables.",
            err=True,
        )
        sys.exit(EXIT_CONFIG_ERROR)
        return

    # Wait for running (optional)
    try:
        if wait:
            wait_for_notebook_running(notebook_id=notebook_id, session=session)
        else:
            get_notebook_detail(notebook_id=notebook_id, session=session)
    except TimeoutError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return

    # Fast-path: Check if we have a cached profile for this notebook and test connectivity
    profile_name = save_as or f"notebook-{notebook_id[:8]}"
    cached_config = load_tunnel_config()
    if profile_name in cached_config.bridges:
        cached_bridge = cached_config.bridges[profile_name]
        # Test if the cached tunnel still works by trying a quick SSH connection
        import subprocess
        test_args = get_ssh_command_args(
            bridge_name=profile_name,
            config=cached_config,
            remote_command="echo ok",
        )
        try:
            result = subprocess.run(
                test_args,
                capture_output=True,
                timeout=10,
                text=True,
            )
            if result.returncode == 0 and "ok" in result.stdout:
                click.echo("Using cached tunnel connection (fast path).", err=True)
                # Reuse the cached config
                args = get_ssh_command_args(
                    bridge_name=profile_name,
                    config=cached_config,
                    remote_command=command,
                )
                os.execvp("ssh", args)
                return  # execvp doesn't return, but for clarity
        except (subprocess.TimeoutExpired, Exception):
            pass  # Fall through to full setup

    # Load SSH public key
    try:
        ssh_public_key = _load_ssh_public_key(pubkey)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    # Set up rtunnel + sshd in notebook and derive proxy URL from Jupyter
    # Pass rtunnel_bin to setup function via environment variable if specified
    if rtunnel_bin:
        os.environ["INSPIRE_RTUNNEL_BIN"] = rtunnel_bin
    try:
        proxy_url = setup_notebook_rtunnel(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            session=session,
            headless=not debug_playwright,
            timeout=setup_timeout,
        )
    except Exception as e:
        click.echo(f"Error setting up notebook tunnel: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return

    # Build a bridge profile for this notebook
    # profile_name already set above in fast-path check
    bridge = BridgeProfile(
        name=profile_name,
        proxy_url=proxy_url,
        ssh_user="root",
        ssh_port=ssh_port,
    )

    # Always save the profile for future fast-path use
    config = load_tunnel_config()
    config.add_bridge(bridge)
    save_tunnel_config(config)
    if save_as:
        click.echo(f"Saved notebook tunnel as profile: {profile_name}", err=True)

    args = get_ssh_command_args(
        bridge_name=profile_name,
        config=config,
        remote_command=command,
    )

    # Replace current process with ssh for interactive behavior
    os.execvp("ssh", args)
