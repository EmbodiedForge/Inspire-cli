"""Resource commands for Inspire CLI."""

import logging
import os
import sys
import time
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_API_ERROR,
)
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.resources import (
    fetch_resource_availability,
    clear_availability_cache,
    ComputeGroupAvailability,
    _normalize_gpu_type,
    KNOWN_COMPUTE_GROUPS,
)
from inspire.cli.utils.web_session import get_web_session, fetch_workspace_availability
from inspire.cli.formatters import json_formatter, human_formatter


@click.group()
def resources():
    """View available compute resources."""
    pass


@resources.command("list")
@click.option(
    "--no-cache",
    is_flag=True,
    help="Bypass cache and fetch fresh availability data",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Thorough check: show all accessible compute groups",
)
@click.option(
    "--gpu-type",
    type=str,
    help="Filter by GPU type (e.g., H100, H200)",
)
@click.option(
    "--watch",
    "-w",
    is_flag=True,
    help="Continuously watch availability (refreshes every 30s)",
)
@click.option(
    "--interval",
    "-i",
    type=int,
    default=30,
    help="Watch refresh interval in seconds (default: 30)",
)
@click.option(
    "--workspace",
    "-ws",
    is_flag=True,
    help="Use browser API to show workspace-scoped availability (requires INSPIRE_USERNAME/PASSWORD)",
)
@pass_context
def list_resources(
    ctx: Context,
    no_cache: bool,
    show_all: bool,
    gpu_type: str,
    watch: bool,
    interval: int,
    workspace: bool = False,
):
    """List GPU availability across compute groups.

    Shows real-time GPU availability for all compute groups via OpenAPI.

    \b
    By default, shows global availability across all projects. Use --workspace
    to show workspace-scoped availability (matches browser, requires login).

    \b
    Examples:
        inspire resources list              # Global OpenAPI view (default)
        inspire resources list --gpu-type H200  # Filter by GPU type
        inspire resources list --workspace  # Workspace-scoped (browser API)
        inspire resources list --all        # Include all compute groups
        inspire resources list --no-cache
        inspire resources list --watch
        inspire resources list --watch --interval 10
    """
    # Watch mode
    if watch:
        if ctx.json_output:
            click.echo(json_formatter.format_json_error(
                "InvalidOption", "Watch mode not supported with JSON output", EXIT_CONFIG_ERROR
            ), err=True)
            sys.exit(EXIT_CONFIG_ERROR)

        _watch_resources(ctx, show_all, interval, workspace, gpu_type)
        return

    # --workspace: browser API for workspace-scoped view
    if workspace:
        _list_workspace_resources(ctx, show_all, gpu_type)
        return

    # Default: OpenAPI global view
    try:
        config = Config.from_env()

        # Clear cache if requested
        if no_cache:
            clear_availability_cache()

        # Fetch availability
        availability = fetch_resource_availability(
            config,
            known_only=not show_all,
        )

        # Filter by GPU type if specified
        if gpu_type:
            availability = [a for a in availability
                          if a.gpu_type.upper() == gpu_type.upper()]

        if not availability:
            if ctx.json_output:
                click.echo(json_formatter.format_json({"availability": []}))
            else:
                click.echo(human_formatter.format_error("No GPU resources found"))
            return

        if ctx.json_output:
            # Format as JSON
            output = [
                {
                    "group_id": a.group_id,
                    "group_name": a.group_name,
                    "gpu_type": a.gpu_type,
                    "gpus_per_node": a.gpu_per_node,
                    "total_nodes": a.total_nodes,
                    "ready_nodes": a.ready_nodes,
                    "free_nodes": a.free_nodes,
                    "free_gpus": a.free_gpus,
                }
                for a in availability
            ]
            click.echo(json_formatter.format_json({"availability": output}))
        else:
            # Format as table
            _format_availability_table(availability)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@resources.command("nodes")
@click.option("--pool", type=click.Choice(["online", "backup", "fault", "unknown"]),
              help="Filter by resource pool")
@click.option("--page", type=int, default=1, help="Page number (default: 1)")
@click.option("--size", type=int, default=20, help="Page size (default: 20)")
@pass_context
def list_nodes(ctx: Context, pool: str, page: int, size: int):
    """List individual cluster nodes.

    Shows available nodes in the cluster, optionally filtered by resource pool.

    \b
    Examples:
        inspire resources nodes
        inspire resources nodes --pool online
        inspire resources nodes --pool fault --size 50
    """
    try:
        config = Config.from_env()
        api = AuthManager.get_api(config)

        result = api.list_cluster_nodes(
            page_num=page,
            page_size=size,
            resource_pool=pool,
        )

        nodes_data = result.get("data", {}).get("nodes", [])
        total = result.get("data", {}).get("total", len(nodes_data))

        if ctx.json_output:
            click.echo(json_formatter.format_json({
                "nodes": nodes_data,
                "total": total,
                "page": page,
                "page_size": size,
                "pool_filter": pool,
            }))
        else:
            click.echo(human_formatter.format_nodes(nodes_data, total))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _list_workspace_resources(ctx: Context, show_all: bool, gpu_type: Optional[str] = None) -> None:
    """List workspace-specific GPU availability using browser API.

    In workspace mode, we show all accessible groups by default since the
    workspace API already scopes to the user's accessible resources.
    """
    try:
        # Get web session (logs in via Playwright if needed)
        # Require workspace_id, will force re-login if missing
        session = get_web_session(require_workspace=True)

        if not session.workspace_id:
            click.echo(human_formatter.format_error(
                "Failed to get workspace_id. Please try again."
            ), err=True)
            sys.exit(EXIT_AUTH_ERROR)

        # Fetch workspace nodes
        nodes = fetch_workspace_availability(session)

        if not nodes:
            click.echo(human_formatter.format_error("No GPU resources found in your workspace"))
            return

        # Group by logic_compute_group_id
        # In workspace mode, show all groups (API already scopes to workspace)
        groups: dict[str, dict] = {}
        for node in nodes:
            if node.get("gpu_count", 0) == 0:
                continue

            group_id = node.get("logic_compute_group_id", "")
            if not group_id:
                continue

            if group_id not in groups:
                gpu_info = node.get("gpu_info", {})
                gpu_display = gpu_info.get("gpu_type_display", "Unknown")
                gpu_type = _normalize_gpu_type(gpu_display)

                # Use known group name if available, otherwise fall back to API name
                group_name = node.get("logic_compute_group_name", "")
                if not group_name and group_id in KNOWN_COMPUTE_GROUPS:
                    group_name = KNOWN_COMPUTE_GROUPS[group_id]
                if not group_name:
                    group_name = "Unknown"

                groups[group_id] = {
                    "group_id": group_id,
                    "group_name": group_name,
                    "gpu_type": gpu_type,
                    "gpu_per_node": node.get("gpu_count", 0),
                    "total_nodes": 0,
                    "ready_nodes": 0,
                    "free_nodes": 0,
                }

            groups[group_id]["total_nodes"] += 1

            if node.get("status") == "READY":
                groups[group_id]["ready_nodes"] += 1

                task_list = node.get("task_list", [])
                if not task_list or len(task_list) == 0:
                    groups[group_id]["free_nodes"] += 1

        # Convert to ComputeGroupAvailability
        availability_list = []
        for group_data in groups.values():
            free_gpus = group_data["free_nodes"] * group_data["gpu_per_node"]
            availability_list.append(
                ComputeGroupAvailability(
                    group_id=group_data["group_id"],
                    group_name=group_data["group_name"],
                    gpu_type=group_data["gpu_type"],
                    gpu_per_node=group_data["gpu_per_node"],
                    total_nodes=group_data["total_nodes"],
                    ready_nodes=group_data["ready_nodes"],
                    free_nodes=group_data["free_nodes"],
                    free_gpus=free_gpus,
                )
            )

        # Sort by free_gpus descending
        availability_list.sort(key=lambda x: x.free_gpus, reverse=True)

        # Filter by GPU type if specified
        if gpu_type:
            availability_list = [a for a in availability_list
                                if a.gpu_type.upper() == gpu_type.upper()]

        # Add workspace indicator
        _format_availability_table(availability_list, workspace_mode=True)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _watch_resources(
    ctx: Context,
    show_all: bool,
    interval: int,
    workspace: bool,
    gpu_type: Optional[str] = None,
) -> None:
    """Watch resources with periodic refresh and progress bar."""
    from datetime import datetime

    # Suppress API logging during watch mode
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    # --workspace: browser API; default: OpenAPI
    use_workspace = workspace
    web_session = None

    if use_workspace:
        try:
            web_session = get_web_session(require_workspace=True)
            if not web_session.workspace_id:
                click.echo(human_formatter.format_error(
                    "Failed to get workspace_id. Login failed or session expired."
                ), err=True)
                sys.exit(EXIT_AUTH_ERROR)
        except Exception as e:
            click.echo(human_formatter.format_error(f"Failed to get web session: {e}"), err=True)
            sys.exit(EXIT_AUTH_ERROR)
    else:
        try:
            config = Config.from_env()
        except ConfigError as e:
            click.echo(human_formatter.format_error(str(e)), err=True)
            sys.exit(EXIT_CONFIG_ERROR)

    def _progress_bar(current: int, total: int, width: int = 20) -> str:
        """Generate a cute progress bar."""
        if total == 0:
            return "░" * width
        filled = int(width * current / total)
        return "█" * filled + "░" * (width - filled)

    # State for progress updates
    progress_state = {"fetched": 0, "total": 0}

    def _render_display(
        availability: list,
        phase: str,
        timestamp: str,
        ws_mode: bool,
    ) -> None:
        """Clear screen and render availability table."""
        os.system('clear')

        if phase == "fetching":
            fetched = progress_state["fetched"]
            total = progress_state["total"] or 1
            bar = _progress_bar(fetched, total)
            if total > 1:
                click.echo(f"🔄 [{bar}] Fetching {fetched}/{total} nodes...\n")
            else:
                click.echo(f"🔄 [{bar}] Fetching availability...\n")
        else:
            bar = _progress_bar(1, 1)
            scope = "(Workspace)" if ws_mode else "(Global)"
            click.echo(f"✅ [{bar}] Updated at {timestamp} {scope} (interval: {interval}s)\n")

        if not availability:
            if phase != "fetching":
                click.echo("No GPU resources found")
            return

        # Compact table with fixed-width columns
        click.echo("─" * 60)
        click.echo(f"{'GPU':<6} {'Location':<24} {'Ready':>8} {'Free':>8} {'GPUs':>8}")
        click.echo("─" * 60)

        total_free = 0
        for a in availability:
            location = a.group_name[:23]
            gpu = a.gpu_type[:5]
            free_gpus = a.free_gpus
            total_free += free_gpus

            # Status indicator
            if free_gpus >= 64:
                indicator = "🟢"
            elif free_gpus >= 16:
                indicator = "🟡"
            elif free_gpus > 0:
                indicator = "🟠"
            else:
                indicator = "🔴"

            click.echo(
                f"{gpu:<6} {location:<24} {a.ready_nodes:>8} {a.free_nodes:>8} {free_gpus:>8} {indicator}"
            )

        click.echo("─" * 60)
        click.echo(f"{'Total':<6} {'':<24} {'':>8} {'':>8} {total_free:>8}")
        click.echo("")
        click.echo("Ctrl+C to stop")

    def on_progress(fetched: int, total: int) -> None:
        """Callback for fetch progress updates."""
        progress_state["fetched"] = fetched
        progress_state["total"] = total
        now = datetime.now().strftime("%H:%M:%S")
        _render_display(availability, "fetching", now, use_workspace)

    def _fetch_workspace_availability() -> list:
        """Fetch and process workspace availability."""
        nodes = fetch_workspace_availability(web_session)
        if not nodes:
            return []

        # Group by logic_compute_group_id
        # In workspace mode, show all groups (API already scopes to workspace)
        groups: dict[str, dict] = {}
        for node in nodes:
            if node.get("gpu_count", 0) == 0:
                continue

            group_id = node.get("logic_compute_group_id", "")
            if not group_id:
                continue

            if group_id not in groups:
                gpu_info = node.get("gpu_info", {})
                gpu_display = gpu_info.get("gpu_type_display", "Unknown")
                gpu_type = _normalize_gpu_type(gpu_display)

                # Use known group name if available, otherwise fall back to API name
                group_name = node.get("logic_compute_group_name", "")
                if not group_name and group_id in KNOWN_COMPUTE_GROUPS:
                    group_name = KNOWN_COMPUTE_GROUPS[group_id]
                if not group_name:
                    group_name = "Unknown"

                groups[group_id] = {
                    "group_id": group_id,
                    "group_name": group_name,
                    "gpu_type": gpu_type,
                    "gpu_per_node": node.get("gpu_count", 0),
                    "total_nodes": 0,
                    "ready_nodes": 0,
                    "free_nodes": 0,
                }

            groups[group_id]["total_nodes"] += 1

            if node.get("status") == "READY":
                groups[group_id]["ready_nodes"] += 1

                task_list = node.get("task_list", [])
                if not task_list or len(task_list) == 0:
                    groups[group_id]["free_nodes"] += 1

        # Convert to ComputeGroupAvailability
        availability_list = []
        for group_data in groups.values():
            free_gpus = group_data["free_nodes"] * group_data["gpu_per_node"]
            availability_list.append(
                ComputeGroupAvailability(
                    group_id=group_data["group_id"],
                    group_name=group_data["group_name"],
                    gpu_type=group_data["gpu_type"],
                    gpu_per_node=group_data["gpu_per_node"],
                    total_nodes=group_data["total_nodes"],
                    ready_nodes=group_data["ready_nodes"],
                    free_nodes=group_data["free_nodes"],
                    free_gpus=free_gpus,
                )
            )

        availability_list.sort(key=lambda x: x.free_gpus, reverse=True)
        return availability_list

    try:
        availability: list = []
        while True:
            # Reset progress
            progress_state["fetched"] = 0
            progress_state["total"] = 0

            # Show initial fetching state
            now = datetime.now().strftime("%H:%M:%S")
            _render_display(availability, "fetching", now, use_workspace)

            try:
                if use_workspace:
                    availability = _fetch_workspace_availability()
                else:
                    # Clear cache to get fresh data
                    clear_availability_cache()
                    availability = fetch_resource_availability(
                        config,
                        known_only=not show_all,
                        progress_callback=on_progress,
                    )

                # Filter by GPU type if specified
                if gpu_type:
                    availability = [a for a in availability
                                  if a.gpu_type.upper() == gpu_type.upper()]
            except AuthenticationError as e:
                api_logger.setLevel(original_level)
                click.echo(human_formatter.format_error(str(e)), err=True)
                sys.exit(EXIT_AUTH_ERROR)
            except Exception as e:
                # Show error but keep retrying
                os.system('clear')
                click.echo(f"⚠️  API error: {e}")
                click.echo(f"Retrying in {interval}s...")
                time.sleep(interval)
                continue

            # Show updated state
            now = datetime.now().strftime("%H:%M:%S")
            _render_display(availability, "done", now, use_workspace)

            # Wait for next refresh
            time.sleep(interval)

    except KeyboardInterrupt:
        click.echo("\nStopped watching.")
        sys.exit(0)
    finally:
        api_logger.setLevel(original_level)


def _format_availability_table(availability, workspace_mode: bool = False) -> None:
    """Format availability as a pretty table."""
    title = "\U0001f4ca GPU Availability (Workspace)" if workspace_mode else "\U0001f4ca GPU Availability (Live)"
    scope_note = "Shows availability in your workspace only" if workspace_mode else ""

    lines = [
        "",
        title,
        "\u2500" * 80,
    ]

    if scope_note:
        lines.append(f"{scope_note}")
        lines.append("\u2500" * 80)

    lines.append(
        f"{'GPU Type':<12} {'Location':<25} {'Ready':<8} {'Free':<8} {'Free GPUs':<12}",
    )
    lines.append("\u2500" * 80)

    for a in availability:
        # Format location name
        location = a.group_name[:24]

        # Format GPU type
        gpu_type = a.gpu_type[:11]

        # Status indicator
        free_gpus = a.free_gpus
        if free_gpus >= 100:
            status = ""
        elif free_gpus >= 32:
            status = ""
        elif free_gpus >= 8:
            status = ""
        elif free_gpus > 0:
            status = "⚠"
        else:
            status = "✗"

        lines.append(
            f"{gpu_type:<12} {location:<25} {a.ready_nodes:<8} {a.free_nodes:<8} {free_gpus:<12} {status}"
        )

    lines.append("\u2500" * 80)
    lines.append("")
    lines.append("\U0001f4a1 Usage:")
    lines.append("  inspire run \"python train.py\"              # Auto-select best group")
    lines.append("  inspire run \"python train.py\" --type H100   # Prefer H100")
    lines.append("  inspire run \"python train.py\" --gpus 4      # Use 4 GPUs")
    lines.append("")

    click.echo("\n".join(lines))


def _handle_error(ctx: Context, error_type: str, message: str, exit_code: int):
    """Handle and format errors consistently."""
    if ctx.json_output:
        click.echo(json_formatter.format_json_error(error_type, message, exit_code), err=True)
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(exit_code)
