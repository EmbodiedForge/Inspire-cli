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
from typing import Optional

import click
import requests
from playwright.sync_api import sync_playwright

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_API_ERROR,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.web_session import get_web_session, get_playwright_proxy


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

    # First try a direct requests call using stored cookies (honors proxy env)
    cookies = (session.storage_state or {}).get("cookies") if session else None
    if cookies:
        s = requests.Session()
        proxy_url = os.environ.get("https_proxy") or os.environ.get("http_proxy")
        if proxy_url:
            s.proxies.update({"http": proxy_url, "https": proxy_url})
        for c in cookies:
            try:
                s.cookies.set(c.get("name"), c.get("value"), domain=c.get("domain"), path=c.get("path", "/"))
            except Exception:
                continue
        try:
            # Bootstrap SSO cookies via /login (keycloak cookies usually allow silent auth)
            try:
                s.get(f"{base_url}/login", timeout=20, allow_redirects=True)
            except Exception:
                pass

            resp = s.get(
                f"{base_url}/api/v1/notebook/list",
                params={"workspace_id": workspace_id},
                headers={"Accept": "application/json", "Referer": f"{base_url}/lab"},
                timeout=20,
                allow_redirects=False,
            )
            if resp.status_code != 200:
                click.echo(f"requests path: status {resp.status_code}", err=True)
            else:
                data = resp.json()
                items = data.get("data", {}).get("items", [])
                if data.get("code") == 0:
                    _print_notebook_list(items, json_output, ctx)
                    return
                if data.get("message") == "notebook not found":
                    if json_output:
                        click.echo(json.dumps({"items": [], "total": 0}))
                    else:
                        click.echo("No notebook instances found.")
                        click.echo(
                            "\nYou can create notebook instances through the Bridge web UI at:\n"
                            f"  {base_url}/lab\n"
                            "Once created, they will appear here."
                        )
                    return
                click.echo(f"requests path: api error {data.get('message', 'unknown')} (code={data.get('code')})", err=True)
        except Exception as e:
            click.echo(f"requests path error: {e}", err=True)

    # If we reach here, requests path failed; report and exit.
    click.echo("requests path: fell through; check auth/proxy", err=True)
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

    proxy = get_playwright_proxy()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy)
            context = browser.new_context(storage_state=session.storage_state, proxy=proxy, ignore_https_errors=True)

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


def _parse_resource_string(resource: str) -> tuple[int, str]:
    """Parse a resource string like '1xH200' into (gpu_count, gpu_type).

    Supported formats:
    - "1xH200", "4xH200", "8xH100"
    - "H200", "H100" (defaults to 1 GPU)
    - "1 H200", "4 H100"

    Returns:
        Tuple of (gpu_count, gpu_type_pattern).
    """
    resource = resource.strip().upper()

    # Pattern: NxGPU (e.g., "1xH200", "4xH100")
    match = re.match(r"^(\d+)\s*[xX]\s*(\w+)$", resource)
    if match:
        return int(match.group(1)), match.group(2)

    # Pattern: N GPU (e.g., "1 H200", "4 H100")
    match = re.match(r"^(\d+)\s+(\w+)$", resource)
    if match:
        return int(match.group(1)), match.group(2)

    # Pattern: GPU only (e.g., "H200") - defaults to 1
    match = re.match(r"^(\w+)$", resource)
    if match:
        return 1, match.group(1)

    raise ValueError(f"Invalid resource format: {resource}")


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


@notebook.command("create")
@click.option(
    "--name", "-n",
    help="Notebook name (auto-generated if omitted)",
)
@click.option(
    "--resource", "-r",
    default="1xH200",
    help="Resource spec (e.g., 1xH200, 4xH100)",
)
@click.option(
    "--project", "-p",
    help="Project name or ID",
)
@click.option(
    "--image", "-i",
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
        gpu_count, gpu_pattern = _parse_resource_string(resource)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    if not json_output:
        click.echo(f"Creating notebook with {gpu_count}x {gpu_pattern}...")

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

    # Find compute group with matching GPU type
    selected_group = None
    selected_gpu_type = None
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

    if not selected_group:
        click.echo(f"Error: No compute group found with GPU type matching '{gpu_pattern}'", err=True)
        click.echo("\nAvailable GPU types:", err=True)
        for group in compute_groups:
            for stats in group.get("gpu_type_stats", []):
                gpu_type = stats.get("gpu_info", {}).get("gpu_type_display", "Unknown")
                click.echo(f"  - {gpu_type}", err=True)
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

    # Find quota matching GPU type and count
    selected_quota = None
    for quota in quota_list:
        if quota.get("gpu_type") == selected_gpu_type and quota.get("gpu_count") == gpu_count:
            selected_quota = quota
            break

    if not selected_quota:
        click.echo(f"Error: No quota found for {gpu_count}x {selected_gpu_type}", err=True)
        click.echo("\nAvailable quotas:", err=True)
        for q in quota_list:
            click.echo(f"  - {q.get('gpu_count')}x {q.get('gpu_type')} ({q.get('name')})", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
        return

    quota_id = selected_quota.get("id", "")
    cpu_count = selected_quota.get("cpu_count", 20)
    memory_size = selected_quota.get("memory_size", 200)

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
                "resource": f"{gpu_count}x{gpu_pattern}",
                "project": selected_project.name,
                "image": selected_image.name,
            }, indent=2))
        else:
            click.echo(f"\nNotebook created successfully!")
            click.echo(f"  ID: {notebook_id}")
            click.echo(f"  Name: {name}")
            click.echo(f"  Resource: {gpu_count}x {gpu_pattern}")
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
