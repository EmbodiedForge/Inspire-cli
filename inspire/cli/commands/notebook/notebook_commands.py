"""Notebook subcommands."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import click

from .notebook_create_flow import run_notebook_create
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.config import ConfigError
from inspire.config.ssh_runtime import resolve_ssh_runtime_config
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"

_NOTEBOOK_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _unique_workspace_ids(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        if not value or value == _ZERO_WORKSPACE_ID:
            continue
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _sort_notebook_items(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)


def _looks_like_notebook_id(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if value.startswith("notebook-"):
        return True
    return bool(_NOTEBOOK_UUID_RE.match(value))


def _notebook_id_from_item(item: dict) -> str | None:
    notebook_id = item.get("notebook_id") or item.get("id")
    if not notebook_id:
        return None
    return str(notebook_id)


def _format_notebook_resource(item: dict) -> str:
    quota = item.get("quota") or {}
    gpu_count = quota.get("gpu_count", 0)

    if gpu_count and gpu_count > 0:
        gpu_info = (item.get("resource_spec_price") or {}).get("gpu_info") or {}
        gpu_type = gpu_info.get("gpu_product_simple") or quota.get("gpu_type") or "GPU"
        return f"{gpu_count}x{gpu_type}"

    cpu_count = quota.get("cpu_count", 0)
    if cpu_count:
        return f"{cpu_count}xCPU"
    return "N/A"


def _try_get_current_user_ids(
    session: web_session_module.WebSession, *, base_url: str
) -> list[str]:
    try:
        user_data = web_session_module.request_json(
            session,
            "GET",
            f"{base_url}/api/v1/user/detail",
            timeout=30,
        )
        user_id = user_data.get("data", {}).get("id")
        if user_id:
            return [str(user_id)]
    except Exception:
        pass
    return []


def _list_notebooks_for_workspace(
    session: web_session_module.WebSession,
    *,
    base_url: str,
    workspace_id: str,
    user_ids: list[str],
    keyword: str = "",
) -> list[dict]:
    body = {
        "workspace_id": workspace_id,
        "page": 1,
        "page_size": 100,
        "filter_by": {
            "keyword": keyword,
            "user_id": user_ids,
            "logic_compute_group_id": [],
            "status": [],
            "mirror_url": [],
        },
        "order_by": [{"field": "created_at", "order": "desc"}],
    }

    data = web_session_module.request_json(
        session,
        "POST",
        f"{base_url}/api/v1/notebook/list",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        message = data.get("message", "Unknown error")
        raise ValueError(f"API error: {message}")

    items = data.get("data", {}).get("list", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _resolve_notebook_id(
    ctx: Context,
    *,
    session: web_session_module.WebSession,
    config,
    base_url: str,
    identifier: str,
    json_output: bool,
) -> tuple[str, str | None]:
    identifier = identifier.strip()
    if not identifier:
        _handle_error(
            ctx,
            "ValidationError",
            "Notebook identifier cannot be empty",
            EXIT_VALIDATION_ERROR,
        )

    if _looks_like_notebook_id(identifier):
        return identifier, None

    candidates: list[str] = []
    for ws_id in (
        getattr(config, "workspace_cpu_id", None),
        getattr(config, "workspace_gpu_id", None),
        getattr(config, "workspace_internet_id", None),
        getattr(config, "job_workspace_id", None),
    ):
        if ws_id:
            candidates.append(str(ws_id))
    workspaces_map = getattr(config, "workspaces", None)
    if isinstance(workspaces_map, dict):
        candidates.extend(str(v) for v in workspaces_map.values() if v)
    if getattr(session, "workspace_id", None):
        candidates.append(str(session.workspace_id))

    workspace_ids = _unique_workspace_ids(candidates)
    if not workspace_ids:
        resolved = None
        try:
            resolved = select_workspace_id(config)
        except Exception:
            resolved = None

        resolved = resolved or getattr(session, "workspace_id", None)
        resolved = None if resolved == _ZERO_WORKSPACE_ID else resolved
        if resolved:
            workspace_ids = [str(resolved)]

    if not workspace_ids:
        _handle_error(
            ctx,
            "ConfigError",
            "No workspace_id configured or available for notebook lookup.",
            EXIT_CONFIG_ERROR,
            hint=(
                "Set [workspaces].cpu/[workspaces].gpu in config.toml, set INSPIRE_WORKSPACE_ID, "
                "or pass a notebook ID directly."
            ),
        )

    user_ids = _try_get_current_user_ids(session, base_url=base_url)

    matches: list[tuple[str, dict]] = []
    for ws_id in workspace_ids:
        try:
            items = _list_notebooks_for_workspace(
                session,
                base_url=base_url,
                workspace_id=ws_id,
                user_ids=user_ids,
                keyword=identifier,
            )
        except Exception:
            continue

        for item in items:
            if str(item.get("name") or "") == identifier:
                matches.append((ws_id, item))

    matches.sort(key=lambda m: str(m[1].get("created_at") or ""), reverse=True)

    if not matches:
        _handle_error(
            ctx,
            "APIError",
            f"Notebook not found: {identifier}",
            EXIT_API_ERROR,
            hint="Run 'inspire notebook list --all-workspaces' to find the notebook ID.",
        )

    if len(matches) == 1:
        ws_id, item = matches[0]
        notebook_id = _notebook_id_from_item(item)
        if not notebook_id:
            _handle_error(
                ctx,
                "APIError",
                f"Notebook '{identifier}' is missing an ID in API response.",
                EXIT_API_ERROR,
            )
        return notebook_id, ws_id

    if json_output:
        ids = [(_notebook_id_from_item(item) or "?") for _, item in matches]
        _handle_error(
            ctx,
            "ValidationError",
            f"Multiple notebooks match name '{identifier}': {', '.join(ids)}",
            EXIT_VALIDATION_ERROR,
            hint="Use a notebook ID instead of a name.",
        )

    click.echo(f"Multiple notebooks named '{identifier}' found:")
    for idx, (ws_id, item) in enumerate(matches, start=1):
        notebook_id = _notebook_id_from_item(item) or "N/A"
        status = str(item.get("status") or "Unknown")
        resource = _format_notebook_resource(item)
        created_at = str(item.get("created_at") or "")
        click.echo(f"  [{idx}] {status:<12} {resource:<12} {notebook_id}  {created_at}  ws={ws_id}")

    choice = click.prompt(
        "Select notebook",
        type=click.IntRange(1, len(matches)),
        default=1,
        show_default=True,
    )
    ws_id, item = matches[choice - 1]
    notebook_id = _notebook_id_from_item(item)
    if not notebook_id:
        _handle_error(
            ctx,
            "APIError",
            f"Notebook '{identifier}' is missing an ID in API response.",
            EXIT_API_ERROR,
        )
    return notebook_id, ws_id


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
@click.argument("notebook")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def stop_notebook_cmd(
    ctx: Context,
    notebook: str,
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
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
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
@click.argument("notebook")
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
    notebook: str,
    wait: bool,
    json_output: bool,
) -> None:
    """Start a stopped notebook instance.

    \b
    Examples:
        inspire notebook start 78822a57-3830-44e7-8d45-e8b0d674fc44
        inspire notebook start ring-8h100-test
        inspire notebook start ring-8h100-test --wait
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Starting notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
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
@click.argument("notebook")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def notebook_status(
    ctx: Context,
    notebook: str,
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
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()

    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    try:
        data = web_session_module.request_json(
            session,
            "GET",
            f"{base_url}/api/v1/notebook/{notebook_id}",
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except ValueError as e:
        message = str(e)
        if "API returned 404" in message:
            _handle_error(
                ctx,
                "NotFound",
                f"Notebook instance '{notebook_id}' not found",
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
    "--all-workspaces",
    is_flag=True,
    help="List notebooks across all configured workspaces (cpu/gpu/internet)",
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
    all_workspaces: bool,
    json_output: bool,
) -> None:
    """List notebook/interactive instances.

    \b
    Examples:
        inspire notebook list
        inspire notebook list --all
        inspire notebook list --workspace-id ws-xxx
        inspire notebook list --workspace gpu
        inspire notebook list --all-workspaces
        inspire notebook list --json
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Listing notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )
    config = load_config(ctx)

    workspace_ids: list[str] = []
    if workspace_id:
        workspace_ids = [workspace_id]
    elif workspace:
        try:
            resolved = select_workspace_id(config, explicit_workspace_name=workspace)
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
        if resolved:
            workspace_ids = [resolved]
    elif all_workspaces:
        candidates: list[str] = []
        for ws_id in (
            config.workspace_cpu_id,
            config.workspace_gpu_id,
            config.workspace_internet_id,
            config.job_workspace_id,
        ):
            if ws_id:
                candidates.append(ws_id)
        if config.workspaces:
            candidates.extend(config.workspaces.values())
        if getattr(session, "workspace_id", None):
            candidates.append(str(session.workspace_id))

        workspace_ids = _unique_workspace_ids(candidates)
        for ws_id in workspace_ids:
            try:
                select_workspace_id(config, explicit_workspace_id=ws_id)
            except ConfigError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
                return

    if not workspace_ids:
        try:
            resolved = select_workspace_id(config)
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        resolved = resolved or getattr(session, "workspace_id", None)
        resolved = None if resolved == _ZERO_WORKSPACE_ID else resolved
        if not resolved:
            _handle_error(
                ctx,
                "ConfigError",
                "No workspace_id configured or provided.",
                EXIT_CONFIG_ERROR,
                hint=(
                    "Use --workspace-id, set [workspaces].cpu/[workspaces].gpu in config.toml, "
                    "or set INSPIRE_WORKSPACE_ID."
                ),
            )
            return
        workspace_ids = [str(resolved)]

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

    all_items: list[dict] = []
    for ws_id in workspace_ids:
        body = {
            "workspace_id": ws_id,
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
                raise ValueError(f"API error: {message}")

            items = data.get("data", {}).get("list", [])
            if isinstance(items, list):
                all_items.extend(items)

        except ValueError as e:
            if len(workspace_ids) == 1:
                _handle_error(
                    ctx,
                    "APIError",
                    str(e),
                    EXIT_API_ERROR,
                    hint="Check auth and proxy configuration.",
                )
                return
            if not ctx.json_output:
                click.echo(f"Warning: workspace {ws_id} failed: {e}", err=True)
            continue
        except Exception as e:
            if len(workspace_ids) == 1:
                _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
                return
            if not ctx.json_output:
                click.echo(f"Warning: workspace {ws_id} failed: {e}", err=True)
            continue

    if not all_items and len(workspace_ids) > 1:
        _handle_error(
            ctx,
            "APIError",
            "Failed to list notebooks from configured workspaces.",
            EXIT_API_ERROR,
        )
        return

    all_items = _sort_notebook_items(all_items)
    _print_notebook_list(all_items, json_output)


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
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook_id,
        json_output=False,
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

    try:
        ssh_runtime = resolve_ssh_runtime_config(
            cli_overrides={"rtunnel_bin": rtunnel_bin},
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    if ssh_runtime.dropbear_deb_dir and not ssh_runtime.setup_script:
        _handle_error(
            ctx,
            "ConfigError",
            "Missing SSH setup script: ssh.setup_script (or INSPIRE_SETUP_SCRIPT) is required "
            "when ssh.dropbear_deb_dir is configured.",
            EXIT_CONFIG_ERROR,
            hint=(
                "Set [ssh].setup_script in config.toml or export INSPIRE_SETUP_SCRIPT to the "
                "setup script path on the cluster."
            ),
        )
        return

    try:
        proxy_url = browser_api_module.setup_notebook_rtunnel(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            ssh_runtime=ssh_runtime,
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
@click.argument("notebook")
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
    notebook: str,
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
        notebook_id=notebook,
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
