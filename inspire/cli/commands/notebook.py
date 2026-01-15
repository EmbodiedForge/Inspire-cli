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

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_API_ERROR,
)
from inspire.cli.formatters import json_formatter


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
    "--json",
    "json_output",
    is_flag=True,
    help="Output as JSON",
)
@pass_context
def list_notebooks(
    ctx: Context,
    workspace_id: Optional[str],
    json_output: bool,
) -> None:
    """List notebook/interactive instances.

    \b
    Examples:
        inspire notebook list
        inspire notebook list --workspace-id ws-xxx
        inspire notebook list --json
    """
    from playwright.sync_api import sync_playwright
    from inspire.cli.utils.web_session import get_web_session, WebSession

    # Get web session for authentication
    try:
        session = get_web_session()
    except ValueError as e:
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
            click.echo(
                "Error: No workspace_id configured or provided. "
                "Use --workspace-id or set INSPIRE_WORKSPACE_ID environment variable.",
                err=True,
            )
            return sys.exit(EXIT_CONFIG_ERROR)

    base_url = "https://qz.sii.edu.cn"

    # Try to get notebook list using GET endpoint
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=session.storage_state)

            try:
                # GET /api/v1/notebook/list works with query parameters
                url = f"{base_url}/api/v1/notebook/list"
                resp = context.request.get(
                    url,
                    params={"workspace_id": workspace_id},
                    headers={
                        "Accept": "application/json",
                        "Referer": f"{base_url}/lab",
                    },
                    timeout=30000,
                )

                if resp.status == 401:
                    raise ValueError("Session expired or invalid")

                data = resp.json()

                # Check response
                if data.get("code") == 0:
                    # Success - we have notebook list
                    items = data.get("data", {}).get("items", [])
                    _print_notebook_list(items, json_output, ctx)
                elif data.get("message") == "notebook not found":
                    # No notebooks exist yet
                    if json_output:
                        click.echo(json.dumps({"items": [], "total": 0}))
                    else:
                        click.echo("No notebook instances found.")
                        click.echo(
                            "\nYou can create notebook instances through the Bridge web UI at:\n"
                            f"  {base_url}/lab\n"
                            "Once created, they will appear here."
                        )
                else:
                    # API error
                    click.echo(f"Error: {data.get('message', 'Unknown error')}", err=True)
                    return sys.exit(EXIT_API_ERROR)

            finally:
                context.close()
                browser.close()

    except Exception as e:
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
            f"\n{'Name':<25} {'Status':<12} {'GPU':<8} {'Created':<20}",
            "-" * 65,
        ]

        for item in items:
            name = item.get("name", "N/A")[:25]
            status = item.get("status", "Unknown")[:12]

            # Try to get GPU info
            gpu_info = "N/A"
            if "resource_spec" in item:
                spec = item["resource_spec"]
                gpu_count = spec.get("gpu_count", 0)
                gpu_type = spec.get("gpu_type", "")
                if gpu_count and gpu_type:
                    gpu_info = f"{gpu_count}x{gpu_type}"
                elif gpu_count == 0:
                    cpu_count = spec.get("cpu_count")
                    if cpu_count:
                        gpu_info = f"{cpu_count}xCPU"

            # Created time
            created = item.get("created_at", "N/A")[:20]

            lines.append(f"{name:<25} {status:<12} {gpu_info:<8} {created:<20}")

        click.echo("\n" + "\n".join(lines))


@notebook.command("status")
@click.argument("instance_id")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Output as JSON",
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
    from playwright.sync_api import sync_playwright
    from inspire.cli.utils.web_session import get_web_session

    try:
        session = get_web_session()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        return sys.exit(EXIT_CONFIG_ERROR)

    base_url = "https://qz.sii.edu.cn"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=session.storage_state)

            try:
                url = f"{base_url}/api/v1/notebook/{instance_id}"
                resp = context.request.get(
                    url,
                    headers={"Accept": "application/json"},
                    timeout=30000,
                )

                if resp.status == 404:
                    click.echo(f"Error: Notebook instance '{instance_id}' not found", err=True)
                    return sys.exit(EXIT_API_ERROR)

                data = resp.json()

                if data.get("code") == 0:
                    notebook = data.get("data", {})
                    if json_output:
                        click.echo(json.dumps(notebook, indent=2, ensure_ascii=False))
                    else:
                        _print_notebook_detail(notebook)
                else:
                    click.echo(f"Error: {data.get('message', 'Unknown error')}", err=True)
                    return sys.exit(EXIT_API_ERROR)

            finally:
                context.close()
                browser.close()

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
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
        or os.environ.get("INSPIRE_IMAGE")
    ),
    help="Image name/URL (prompts interactively if omitted)",
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
    help="Output as JSON",
)
@pass_context
def create_notebook_cmd(
    ctx: Context,
    name: Optional[str],
    resource: str,
    project: Optional[str],
    image: Optional[str],
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
    """
    from inspire.cli.utils.web_session import get_web_session
    from inspire.cli.utils.browser_api import (
        list_projects,
        list_images,
        list_notebook_compute_groups,
        get_notebook_schedule,
        create_notebook,
    )

    # Get web session for authentication
    try:
        session = get_web_session()
    except ValueError as e:
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
        click.echo(f"Error fetching projects: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return

    if not projects:
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
        click.echo(f"Error fetching images: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return

    if not images:
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
            auto_stop=auto_stop,
            workspace_id=workspace_id,
            session=session,
        )

        notebook_id = result.get("notebook_id", "")

        if json_output:
            click.echo(json.dumps({
                "notebook_id": notebook_id,
                "name": name,
                "resource": resource_display,
                "project": selected_project.name,
                "image": selected_image.name,
            }, indent=2))
        else:
            click.echo(f"\nNotebook created successfully!")
            click.echo(f"  ID: {notebook_id}")
            click.echo(f"  Name: {name}")
            click.echo(f"  Resource: {resource_display}")
            click.echo(f"\nUse 'inspire notebook status {notebook_id}' to check status.")

    except Exception as e:
        if json_output:
            click.echo(json_formatter.format_json_error("create_error", str(e)))
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
    help="Output as JSON",
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

    try:
        session = get_web_session()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    try:
        result = stop_notebook(notebook_id=notebook_id, session=session)

        if json_output:
            click.echo(json.dumps({
                "notebook_id": notebook_id,
                "status": "stopping",
                "result": result,
            }, indent=2))
        else:
            click.echo(f"Notebook '{notebook_id}' is being stopped.")
            click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")

    except Exception as e:
        if json_output:
            click.echo(json_formatter.format_json_error("stop_error", str(e)))
        else:
            click.echo(f"Error stopping notebook: {e}", err=True)
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

    # Load SSH public key
    try:
        ssh_public_key = _load_ssh_public_key(pubkey)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    # Set up rtunnel + sshd in notebook and derive proxy URL from Jupyter
    try:
        proxy_url = setup_notebook_rtunnel(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            session=session,
            headless=True,
        )
    except Exception as e:
        click.echo(f"Error setting up notebook tunnel: {e}", err=True)
        sys.exit(EXIT_API_ERROR)
        return

    # Build a bridge profile for this notebook
    profile_name = save_as or f"notebook-{notebook_id[:8]}"
    bridge = BridgeProfile(
        name=profile_name,
        proxy_url=proxy_url,
        ssh_user="root",
        ssh_port=ssh_port,
    )

    if save_as:
        config = load_tunnel_config()
        config.add_bridge(bridge)
        save_tunnel_config(config)
        click.echo(f"Saved notebook tunnel as profile: {profile_name}")
    else:
        config = TunnelConfig(bridges={profile_name: bridge}, default_bridge=profile_name)

    args = get_ssh_command_args(
        bridge_name=profile_name,
        config=config,
        remote_command=command,
    )

    # Replace current process with ssh for interactive behavior
    os.execvp("ssh", args)
