"""Notebook subcommands."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import click

from .notebook_create_flow import run_notebook_create
from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.config import ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module


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
    default=None,
    help="Shared memory size in GB (default: INSPIRE_SHM_SIZE/job.shm_size, else 32)",
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
    shm_size: Optional[int],
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
    run_notebook_create(
        ctx,
        name=name,
        workspace=workspace,
        workspace_id=workspace_id,
        resource=resource,
        project=project,
        image=image,
        shm_size=shm_size,
        auto_stop=auto_stop,
        auto=auto,
        wait=wait,
        keepalive=keepalive,
        json_output=json_output,
    )


@click.command("stop")
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
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Stopping notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

    try:
        result = browser_api_module.stop_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to stop notebook: {e}", EXIT_API_ERROR)
        return

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
        return

    click.echo(f"Notebook '{notebook_id}' is being stopped.")
    click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")


@click.command("start")
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
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Starting notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

    try:
        result = browser_api_module.start_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to start notebook: {e}", EXIT_API_ERROR)
        return

    if not json_output:
        click.echo(f"Notebook '{notebook_id}' is being started.")

    if wait:
        if not json_output:
            click.echo("Waiting for notebook to reach RUNNING status...")
        try:
            browser_api_module.wait_for_notebook_running(notebook_id=notebook_id, session=session)
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
        return

    click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")


@click.command("status")
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
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Notebook status requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()

    try:
        data = web_session_module.request_json(
            session,
            "GET",
            f"{base_url}/api/v1/notebook/{instance_id}",
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except ValueError as e:
        message = str(e)
        if "API returned 404" in message:
            _handle_error(
                ctx,
                "NotFound",
                f"Notebook instance '{instance_id}' not found",
                EXIT_API_ERROR,
            )
        else:
            _handle_error(ctx, "APIError", message, EXIT_API_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if data.get("code") == 0:
        notebook = data.get("data", {})
        if json_output:
            click.echo(json_formatter.format_json(notebook))
        else:
            _print_notebook_detail(notebook)
        return

    _handle_error(
        ctx,
        "APIError",
        data.get("message", "Unknown error"),
        EXIT_API_ERROR,
    )
    return


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

    if "resource_spec" in notebook:
        spec = notebook["resource_spec"]
        fields.extend(
            [
                ("GPU Count", spec.get("gpu_count")),
                ("GPU Type", spec.get("gpu_type")),
                ("CPU", spec.get("cpu_count")),
                ("Memory", spec.get("memory_size")),
            ]
        )

    for label, value in fields:
        if value:
            click.echo(f"  {label:<15}: {value}")

    click.echo(f"{'='*60}\n")


@click.command("list")
@click.option(
    "--workspace",
    help="Workspace name (from [workspaces])",
)
@click.option(
    "--workspace-id",
    help="Workspace ID (defaults to configured workspace)",
)
@click.option(
    "--all",
    "-a",
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
    workspace: Optional[str],
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
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Listing notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )
    config = load_config(ctx)

    if not workspace_id:
        try:
            if workspace:
                workspace_id = select_workspace_id(config, explicit_workspace_name=workspace)
            else:
                workspace_id = select_workspace_id(config)
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        if not workspace_id:
            workspace_id = session.workspace_id

        if workspace_id == "ws-00000000-0000-0000-0000-000000000000":
            workspace_id = None

        if not workspace_id:
            _handle_error(
                ctx,
                "ConfigError",
                "No workspace_id configured or provided.",
                EXIT_CONFIG_ERROR,
                hint=(
                    "Use --workspace-id, set [workspaces].cpu in config.toml, or set "
                    "INSPIRE_WORKSPACE_ID."
                ),
            )
            return

    base_url = get_base_url()

    user_ids: list[str] = []
    if not show_all:
        try:
            user_data = web_session_module.request_json(
                session,
                "GET",
                f"{base_url}/api/v1/user/detail",
                timeout=30,
            )
            user_id = user_data.get("data", {}).get("id")
            if user_id:
                user_ids = [user_id]
        except Exception:
            pass

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
        data = web_session_module.request_json(
            session,
            "POST",
            f"{base_url}/api/v1/notebook/list",
            body=body,
            timeout=30,
        )

        if data.get("code") != 0:
            message = data.get("message", "Unknown error")
            _handle_error(ctx, "APIError", f"API error: {message}", EXIT_API_ERROR)
            return

        items = data.get("data", {}).get("list", [])
        _print_notebook_list(items, json_output)

    except ValueError as e:
        _handle_error(
            ctx,
            "APIError",
            str(e),
            EXIT_API_ERROR,
            hint="Check auth and proxy configuration.",
        )
        return


def _print_notebook_list(items: list, json_output: bool) -> None:
    """Print notebook list in appropriate format."""
    if json_output:
        click.echo(json_formatter.format_json({"items": items, "total": len(items)}))
        return

    if not items:
        click.echo("No notebook instances found.")
        return

    lines = [
        f"{'Name':<25} {'Status':<12} {'Resource':<12} {'ID':<38}",
        "-" * 90,
    ]

    for item in items:
        name = item.get("name", "N/A")[:25]
        status = item.get("status", "Unknown")[:12]
        notebook_id = item.get("notebook_id", item.get("id", "N/A"))

        resource_info = "N/A"
        quota = item.get("quota") or {}
        gpu_count = quota.get("gpu_count", 0)

        if gpu_count and gpu_count > 0:
            gpu_info = (item.get("resource_spec_price") or {}).get("gpu_info") or {}
            gpu_type = gpu_info.get("gpu_product_simple", "GPU")
            resource_info = f"{gpu_count}x{gpu_type}"
        else:
            cpu_count = quota.get("cpu_count", 0)
            if cpu_count:
                resource_info = f"{cpu_count}xCPU"

        lines.append(f"{name:<25} {status:<12} {resource_info:<12} {notebook_id:<38}")

    click.echo("\n".join(lines))


def load_ssh_public_key(pubkey_path: Optional[str] = None) -> str:
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


def run_notebook_ssh(
    ctx: Context,
    *,
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
    from inspire.bridge.tunnel import (
        BridgeProfile,
        get_ssh_command_args,
        has_internet_for_gpu_type,
        load_tunnel_config,
        save_tunnel_config,
    )
    from inspire.cli.utils.notebook_cli import require_web_session

    session = require_web_session(
        ctx,
        hint=(
            "Notebook SSH requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

    try:
        if wait:
            notebook_detail = browser_api_module.wait_for_notebook_running(
                notebook_id=notebook_id, session=session
            )
        else:
            notebook_detail = browser_api_module.get_notebook_detail(
                notebook_id=notebook_id, session=session
            )
    except TimeoutError as e:
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook to reach RUNNING: {e}",
            EXIT_API_ERROR,
        )
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    gpu_info = (notebook_detail.get("resource_spec_price") or {}).get("gpu_info") or {}
    gpu_type = gpu_info.get("gpu_product_simple", "")
    has_internet = has_internet_for_gpu_type(gpu_type)

    profile_name = save_as or f"notebook-{notebook_id[:8]}"
    cached_config = load_tunnel_config()

    if profile_name in cached_config.bridges:
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
                args = get_ssh_command_args(
                    bridge_name=profile_name,
                    config=cached_config,
                    remote_command=command,
                )
                os.execvp("ssh", args)
                return
        except (subprocess.TimeoutExpired, Exception):
            pass

    try:
        ssh_public_key = load_ssh_public_key(pubkey)
    except ValueError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    if rtunnel_bin:
        os.environ["INSPIRE_RTUNNEL_BIN"] = rtunnel_bin

    try:
        proxy_url = browser_api_module.setup_notebook_rtunnel(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            session=session,
            headless=not debug_playwright,
            timeout=setup_timeout,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to set up notebook tunnel: {e}", EXIT_API_ERROR)
        return

    bridge = BridgeProfile(
        name=profile_name,
        proxy_url=proxy_url,
        ssh_user="root",
        ssh_port=ssh_port,
        has_internet=has_internet,
    )

    config = load_tunnel_config()
    config.add_bridge(bridge)
    save_tunnel_config(config)

    internet_status = "yes" if has_internet else "no"
    gpu_label = gpu_type if gpu_type else "CPU"
    click.echo(
        f"Added bridge '{profile_name}' (internet: {internet_status}, GPU: {gpu_label})", err=True
    )

    args = get_ssh_command_args(
        bridge_name=profile_name,
        config=config,
        remote_command=command,
    )

    os.execvp("ssh", args)


@click.command("ssh")
@click.argument("notebook_id")
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for notebook to reach RUNNING status",
)
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help=(
        "SSH public key path to authorize (defaults to ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub)"
    ),
)
@click.option(
    "--save-as",
    help=(
        "Save this notebook tunnel as a named profile (usable with 'ssh <name>' after "
        "'inspire tunnel ssh-config --install')"
    ),
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
    run_notebook_ssh(
        ctx,
        notebook_id=notebook_id,
        wait=wait,
        pubkey=pubkey,
        save_as=save_as,
        port=port,
        ssh_port=ssh_port,
        command=command,
        rtunnel_bin=rtunnel_bin,
        debug_playwright=debug_playwright,
        setup_timeout=setup_timeout,
    )


__all__ = [
    "create_notebook_cmd",
    "list_notebooks",
    "notebook_status",
    "ssh_notebook_cmd",
    "start_notebook_cmd",
    "stop_notebook_cmd",
]
