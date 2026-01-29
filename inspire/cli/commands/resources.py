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
from inspire.cli.utils.resources import (
    fetch_resource_availability,
    clear_availability_cache,
    KNOWN_COMPUTE_GROUPS,
)
from inspire.cli.utils.web_session import SessionExpiredError, get_web_session
from inspire.cli.formatters import json_formatter, human_formatter
from inspire.compute_groups import load_compute_groups_from_config, compute_group_name_map
from inspire.cli.utils.config import Config


@click.group()
def resources():
    """View available compute resources."""
    pass


@resources.command("list")
@click.option(
    "--no-cache",
    is_flag=True,
    help="Bypass cached node availability (workspace view only)",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Thorough check: show all accessible compute groups",
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
    help="Show per-node availability (workspace-scoped, browser API)",
)
@click.option(
    "--global",
    "use_global",
    is_flag=True,
    help="Deprecated: alias for --workspace (OpenAPI view removed)",
)
@pass_context
def list_resources(
    ctx: Context,
    no_cache: bool,
    show_all: bool,
    watch: bool,
    interval: int,
    workspace: bool = False,
    use_global: bool = False,
):
    """List GPU availability across compute groups.

    By default, shows accurate real-time GPU usage via browser API.
    Use --workspace for per-node availability (free/ready nodes).

    \b
    Examples:
        inspire resources list              # Accurate GPU usage (default)
        inspire resources list --workspace  # Node-level availability
        inspire resources list --all        # Include all compute groups
        inspire resources list --watch      # Watch mode
    """
    # Watch mode
    if watch:
        if ctx.json_output:
            click.echo(json_formatter.format_json_error(
                "InvalidOption", "Watch mode not supported with JSON output", EXIT_CONFIG_ERROR
            ), err=True)
            sys.exit(EXIT_CONFIG_ERROR)

        _watch_resources(ctx, show_all, interval, workspace, use_global)
        return

    # --workspace: browser API per-node view
    if workspace or use_global:
        if use_global and not workspace:
            click.echo(
                "Note: --global is deprecated; showing workspace node availability instead.",
                err=True,
            )
        _list_workspace_resources(ctx, show_all, no_cache)
        return

    # Default: accurate browser API
    _list_accurate_resources(ctx, show_all)


@resources.command("nodes")
@click.option("--group", help="Filter by compute group name (partial match)")
@pass_context
def list_nodes(ctx: Context, group: str):
    """Show how many FULL 8-GPU nodes are currently free per compute group.

    This uses the browser-only endpoint POST /api/v1/cluster_nodes/list
    (filtered by logic_compute_group_id), so it accounts for GPU fragmentation
    across nodes.

    \b
    Examples:
        inspire resources nodes
        inspire resources nodes --group H200
    """
    try:
        from inspire.cli.utils.browser_api import (
            get_full_free_node_counts,
            get_accurate_gpu_availability,
        )

        group_ids = list(KNOWN_COMPUTE_GROUPS.keys())
        counts = get_full_free_node_counts(group_ids, gpu_per_node=8)

        # Get accurate GPU availability for matching free GPU counts
        accurate_availability = get_accurate_gpu_availability()
        accurate_map = {a.group_id: a.available_gpus for a in accurate_availability}

        # Fill missing names from KNOWN_COMPUTE_GROUPS and apply filter
        filtered: list[dict] = []
        group_lower = (group or "").lower()
        for c in counts:
            name = c.group_name or KNOWN_COMPUTE_GROUPS.get(c.group_id, c.group_id[-12:])
            if group_lower and group_lower not in name.lower():
                continue
            # Use accurate available GPUs if available, otherwise fall back to computed
            free_gpus = accurate_map.get(c.group_id, c.full_free_nodes * c.gpu_per_node)
            filtered.append({
                "group_id": c.group_id,
                "group_name": name,
                "gpu_per_node": c.gpu_per_node,
                "total_nodes": c.total_nodes,
                "ready_nodes": c.ready_nodes,
                "full_free_nodes": c.full_free_nodes,
                "full_free_gpus": free_gpus,
            })

        # Sort by full_free_nodes descending
        filtered.sort(key=lambda x: x["full_free_nodes"], reverse=True)

        if ctx.json_output:
            click.echo(json_formatter.format_json({
                "groups": filtered,
                "total_full_free_nodes": sum(x["full_free_nodes"] for x in filtered),
            }))
            return

        click.echo("")
        click.echo("📊 Full-Free 8-GPU Nodes by Compute Group")
        click.echo("─" * 78)
        click.echo(f"{'Group':<25} {'Full Free':>10} {'Ready':>8} {'Total':>8} {'Free GPUs':>10}")
        click.echo("─" * 78)

        total_full_free = 0
        total_free_gpus = 0
        for row in filtered:
            name = row["group_name"][:24]
            full_free = row["full_free_nodes"]
            ready = row["ready_nodes"]
            total = row["total_nodes"]
            free_gpus = row["full_free_gpus"]

            total_full_free += full_free
            total_free_gpus += free_gpus

            if full_free >= 10:
                indicator = "🟢"
            elif full_free >= 3:
                indicator = "🟡"
            elif full_free > 0:
                indicator = "🟠"
            else:
                indicator = "🔴"

            click.echo(f"{name:<25} {full_free:>10} {ready:>8} {total:>8} {free_gpus:>10} {indicator}")

        click.echo("─" * 78)
        click.echo(f"{'TOTAL':<25} {total_full_free:>10} {'':>8} {'':>8} {total_free_gpus:>10}")
        click.echo("")
        click.echo("Full Free = READY nodes with 8 GPUs and no running tasks")
        click.echo("Free GPUs = Total available GPUs (matches 'inspire resources list')")
        click.echo("")

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _list_accurate_resources(ctx: Context, show_all: bool) -> None:
    """List accurate GPU availability using browser API.

    Uses /api/v1/compute_resources/logic_compute_groups/{id} to get real-time
    GPU usage statistics including used GPUs, available GPUs, and low-priority usage.
    """
    try:
        from inspire.cli.utils.browser_api import get_accurate_gpu_availability

        # Load known compute groups from config
        known_groups = KNOWN_COMPUTE_GROUPS
        if not show_all:
            try:
                config, _ = Config.from_files_and_env(require_credentials=False)
                if config.compute_groups:
                    groups_tuple = load_compute_groups_from_config(config.compute_groups)
                    known_groups = compute_group_name_map(groups_tuple)
            except Exception:
                pass  # Fall back to global KNOWN_COMPUTE_GROUPS

        # Get accurate GPU stats
        availability = get_accurate_gpu_availability()

        if not show_all:
            availability = [a for a in availability if a.group_id in known_groups]
            for entry in availability:
                if not entry.group_name:
                    entry.group_name = known_groups.get(entry.group_id, entry.group_name)

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
                    "total_gpus": a.total_gpus,
                    "used_gpus": a.used_gpus,
                    "available_gpus": a.available_gpus,
                    "low_priority_gpus": a.low_priority_gpus,
                }
                for a in availability
            ]
            click.echo(json_formatter.format_json({"availability": output}))
        else:
            # Format as table
            _format_accurate_availability_table(availability)

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _list_workspace_resources(ctx: Context, show_all: bool, no_cache: bool) -> None:
    """List workspace-specific GPU availability using browser API.

    In workspace mode, we show all accessible groups by default since the
    workspace API already scopes to the user's accessible resources.
    """
    try:
        if no_cache:
            clear_availability_cache()

        # Load config to get compute_groups
        config = None
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
        except Exception:
            pass

        availability = fetch_resource_availability(
            config=config,
            known_only=not show_all,
        )

        if not availability:
            click.echo(human_formatter.format_error("No GPU resources found in your workspace"))
            return

        if ctx.json_output:
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
            return

        _format_availability_table(availability, workspace_mode=True)

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _watch_resources(
    ctx: Context,
    show_all: bool,
    interval: int,
    workspace: bool,
    use_global: bool,
) -> None:
    """Watch resources with periodic refresh and progress bar."""
    from datetime import datetime

    # Suppress API logging during watch mode
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    # Determine which view to use
    mode = "nodes" if workspace or use_global else "accurate"

    try:
        if mode == "nodes":
            get_web_session(require_workspace=True)
        else:
            get_web_session()
    except Exception as e:
        click.echo(human_formatter.format_error(f"Failed to get web session: {e}"), err=True)
        sys.exit(EXIT_AUTH_ERROR)

    def _progress_bar(current: int, total: int, width: int = 20) -> str:
        """Generate a cute progress bar."""
        if total == 0:
            return "░" * width
        filled = int(width * current / total)
        return "█" * filled + "░" * (width - filled)

    # State for progress updates
    progress_state = {"fetched": 0, "total": 0}

    def _render_nodes_display(availability: list, phase: str, timestamp: str) -> None:
        """Render node-level availability table."""
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
            click.echo(f"✅ [{bar}] Updated at {timestamp} (Workspace) (interval: {interval}s)\n")

        if not availability:
            if phase != "fetching":
                click.echo("No GPU resources found")
            return

        click.echo("─" * 60)
        click.echo(f"{'GPU':<6} {'Location':<24} {'Ready':>8} {'Free':>8} {'GPUs':>8}")
        click.echo("─" * 60)

        total_free = 0
        for a in availability:
            location = a.group_name[:23]
            gpu = a.gpu_type[:5]
            free_gpus = a.free_gpus
            total_free += free_gpus

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

    def _render_accurate_display(availability: list, phase: str, timestamp: str) -> None:
        """Render accurate GPU availability table."""
        os.system('clear')

        if phase == "fetching":
            click.echo("🔄 Fetching accurate availability...\n")
        else:
            click.echo(f"✅ Updated at {timestamp} (Accurate) (interval: {interval}s)\n")

        if not availability:
            if phase != "fetching":
                click.echo("No GPU resources found")
            return

        lines = [
            "─" * 95,
            f"{'GPU Type':<22} {'Compute Group':<25} {'Available':>10} {'Used':>8} {'Low Pri':>8} {'Total':>8}",
            "─" * 95,
        ]

        sorted_avail = sorted(availability, key=lambda x: x.available_gpus, reverse=True)

        total_available = 0
        total_used = 0
        total_low_pri = 0
        total_gpus = 0

        for a in sorted_avail:
            gpu_type = a.gpu_type[:21]
            location = a.group_name[:24]
            free_gpus = a.available_gpus

            if free_gpus >= 100:
                status = "✓"
            elif free_gpus >= 32:
                status = "○"
            elif free_gpus >= 8:
                status = "◐"
            elif free_gpus > 0:
                status = "⚠"
            else:
                status = "✗"

            lines.append(
                f"{gpu_type:<22} {location:<25} {a.available_gpus:>10} {a.used_gpus:>8} {a.low_priority_gpus:>8} {a.total_gpus:>8} {status}"
            )

            total_available += a.available_gpus
            total_used += a.used_gpus
            total_low_pri += a.low_priority_gpus
            total_gpus += a.total_gpus

        lines.append("─" * 95)
        lines.append(
            f"{'TOTAL':<22} {'':<25} {total_available:>10} {total_used:>8} {total_low_pri:>8} {total_gpus:>8}"
        )
        lines.append("")
        lines.append("Ctrl+C to stop")

        click.echo("\n".join(lines))

    def _render_display(availability: list, phase: str, timestamp: str) -> None:
        if mode == "nodes":
            _render_nodes_display(availability, phase, timestamp)
        else:
            _render_accurate_display(availability, phase, timestamp)

    def on_progress(fetched: int, total: int) -> None:
        """Callback for fetch progress updates."""
        if mode != "nodes":
            return
        progress_state["fetched"] = fetched
        progress_state["total"] = total
        now = datetime.now().strftime("%H:%M:%S")
        _render_display(availability, "fetching", now)

    try:
        availability: list = []
        while True:
            # Reset progress
            progress_state["fetched"] = 0
            progress_state["total"] = 0

            # Show initial fetching state
            now = datetime.now().strftime("%H:%M:%S")
            _render_display(availability, "fetching", now)

            try:
                if mode == "nodes":
                    clear_availability_cache()
                    # Load config for compute_groups
                    config = None
                    try:
                        config, _ = Config.from_files_and_env(require_credentials=False)
                    except Exception:
                        pass
                    availability = fetch_resource_availability(
                        config=config,
                        known_only=not show_all,
                        progress_callback=on_progress,
                    )
                else:
                    from inspire.cli.utils.browser_api import get_accurate_gpu_availability

                    availability = get_accurate_gpu_availability()
                    # Load known compute groups from config
                    known_groups = KNOWN_COMPUTE_GROUPS
                    if not show_all:
                        try:
                            cfg, _ = Config.from_files_and_env(require_credentials=False)
                            if cfg.compute_groups:
                                groups_tuple = load_compute_groups_from_config(cfg.compute_groups)
                                known_groups = compute_group_name_map(groups_tuple)
                        except Exception:
                            pass
                    if not show_all:
                        availability = [a for a in availability if a.group_id in known_groups]
                        for entry in availability:
                            if not entry.group_name:
                                entry.group_name = known_groups.get(entry.group_id, entry.group_name)
            except (SessionExpiredError, ValueError) as e:
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
            _render_display(availability, "done", now)

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


def _format_accurate_availability_table(availability) -> None:
    """Format accurate GPU availability as a pretty table."""
    lines = [
        "",
        "📊 GPU Availability (Accurate Real-Time)",
        "─" * 95,
        f"{'GPU Type':<22} {'Compute Group':<25} {'Available':>10} {'Used':>8} {'Low Pri':>8} {'Total':>8}",
        "─" * 95,
    ]

    # Sort by available GPUs descending
    sorted_avail = sorted(availability, key=lambda x: x.available_gpus, reverse=True)

    total_available = 0
    total_used = 0
    total_low_pri = 0
    total_gpus = 0

    for a in sorted_avail:
        gpu_type = a.gpu_type[:21]
        location = a.group_name[:24]

        # Status indicator
        free_gpus = a.available_gpus
        if free_gpus >= 100:
            status = "✓"
        elif free_gpus >= 32:
            status = "○"
        elif free_gpus >= 8:
            status = "◐"
        elif free_gpus > 0:
            status = "⚠"
        else:
            status = "✗"

        lines.append(
            f"{gpu_type:<22} {location:<25} {a.available_gpus:>10} {a.used_gpus:>8} {a.low_priority_gpus:>8} {a.total_gpus:>8} {status}"
        )

        total_available += a.available_gpus
        total_used += a.used_gpus
        total_low_pri += a.low_priority_gpus
        total_gpus += a.total_gpus

    lines.append("─" * 95)
    lines.append(
        f"{'TOTAL':<22} {'':<25} {total_available:>10} {total_used:>8} {total_low_pri:>8} {total_gpus:>8}"
    )
    lines.append("")
    lines.append("💡 Legend:")
    lines.append("  Available = GPUs ready to use (not running any tasks)")
    lines.append("  Used      = GPUs currently running tasks")
    lines.append("  Low Pri   = GPUs running low-priority tasks (can be preempted)")
    lines.append("")
    lines.append("💡 Usage:")
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
