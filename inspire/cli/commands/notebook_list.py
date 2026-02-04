"""Notebook list command."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.notebook_cli import (
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.platform.web import session as web_session_module
from inspire.config import ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config.workspaces import select_workspace_id


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

    # Use workspace_id from session if not provided
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
                hint="Use --workspace-id, set [workspaces].cpu in config.toml, or set INSPIRE_WORKSPACE_ID.",
            )
            return

    base_url = get_base_url()

    # Get current user ID for filtering (unless --all is specified)
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

        # API returns items in data.list (not data.items)
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
