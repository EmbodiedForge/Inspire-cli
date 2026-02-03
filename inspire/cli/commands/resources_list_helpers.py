"""Helpers for `inspire resources list`."""

from __future__ import annotations

import logging
import os
import sys
import time

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_AUTH_ERROR, EXIT_CONFIG_ERROR
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.config import Config
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.resources import (
    KNOWN_COMPUTE_GROUPS,
    clear_availability_cache,
    fetch_resource_availability,
)
from inspire.cli.utils.web_session import SessionExpiredError, get_web_session
from inspire.compute_groups import compute_group_name_map, load_compute_groups_from_config


def run_resources_list(
    ctx: Context,
    *,
    no_cache: bool,
    show_all: bool,
    watch: bool,
    interval: int,
    workspace: bool,
    use_global: bool,
) -> None:
    """Implementation for `inspire resources list`."""
    if watch:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "InvalidOption",
                    "Watch mode not supported with JSON output",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
            sys.exit(EXIT_CONFIG_ERROR)

        _watch_resources(ctx, show_all, interval, workspace, use_global)
        return

    if workspace or use_global:
        if use_global and not workspace:
            click.echo(
                "Note: --global is deprecated; showing workspace node availability instead.",
                err=True,
            )
        _list_workspace_resources(ctx, show_all, no_cache)
        return

    _list_accurate_resources(ctx, show_all)


def _known_compute_groups_from_config(*, show_all: bool) -> dict[str, str]:
    known_groups = KNOWN_COMPUTE_GROUPS
    if show_all:
        return known_groups

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        if config.compute_groups:
            groups_tuple = load_compute_groups_from_config(config.compute_groups)
            return compute_group_name_map(groups_tuple)
    except Exception:
        return known_groups
    return known_groups


def _list_accurate_resources(ctx: Context, show_all: bool) -> None:
    """List accurate GPU availability using browser API."""
    try:
        known_groups = _known_compute_groups_from_config(show_all=show_all)

        availability = browser_api_module.get_accurate_gpu_availability()

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
            _format_accurate_availability_table(availability)

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _list_workspace_resources(ctx: Context, show_all: bool, no_cache: bool) -> None:
    """List workspace-specific GPU availability using browser API."""
    try:
        if no_cache:
            clear_availability_cache()

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
    """Watch resources with periodic refresh and progress display."""
    from datetime import datetime

    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

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
        if total == 0:
            return "░" * width
        filled = int(width * current / total)
        return "█" * filled + "░" * (width - filled)

    progress_state = {"fetched": 0, "total": 0}

    def _render_nodes_display(availability: list, phase: str, timestamp: str) -> None:
        os.system("clear")

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
                f"{gpu:<6} {location:<24} {a.ready_nodes:>8} {a.free_nodes:>8} "
                f"{free_gpus:>8} {indicator}"
            )

        click.echo("─" * 60)
        click.echo(f"{'Total':<6} {'':<24} {'':>8} {'':>8} {total_free:>8}")
        click.echo("")
        click.echo("Ctrl+C to stop")

    def _render_accurate_display(availability: list, phase: str, timestamp: str) -> None:
        os.system("clear")

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
            (
                f"{'GPU Type':<22} {'Compute Group':<25} {'Available':>10} "
                f"{'Used':>8} {'Low Pri':>8} {'Total':>8}"
            ),
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
                f"{gpu_type:<22} {location:<25} {a.available_gpus:>10} {a.used_gpus:>8} "
                f"{a.low_priority_gpus:>8} {a.total_gpus:>8} {status}"
            )

            total_available += a.available_gpus
            total_used += a.used_gpus
            total_low_pri += a.low_priority_gpus
            total_gpus += a.total_gpus

        lines.append("─" * 95)
        lines.append(
            f"{'TOTAL':<22} {'':<25} {total_available:>10} {total_used:>8} "
            f"{total_low_pri:>8} {total_gpus:>8}"
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
        if mode != "nodes":
            return
        progress_state["fetched"] = fetched
        progress_state["total"] = total
        now = datetime.now().strftime("%H:%M:%S")
        _render_display(availability, "fetching", now)

    try:
        availability: list = []
        while True:
            progress_state["fetched"] = 0
            progress_state["total"] = 0

            now = datetime.now().strftime("%H:%M:%S")
            _render_display(availability, "fetching", now)

            try:
                if mode == "nodes":
                    clear_availability_cache()
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
                    availability = browser_api_module.get_accurate_gpu_availability()
                    known_groups = _known_compute_groups_from_config(show_all=show_all)
                    if not show_all:
                        availability = [a for a in availability if a.group_id in known_groups]
                        for entry in availability:
                            if not entry.group_name:
                                entry.group_name = known_groups.get(
                                    entry.group_id, entry.group_name
                                )
            except (SessionExpiredError, ValueError) as e:
                api_logger.setLevel(original_level)
                click.echo(human_formatter.format_error(str(e)), err=True)
                sys.exit(EXIT_AUTH_ERROR)
            except Exception as e:
                os.system("clear")
                click.echo(f"⚠️  API error: {e}")
                click.echo(f"Retrying in {interval}s...")
                time.sleep(interval)
                continue

            now = datetime.now().strftime("%H:%M:%S")
            _render_display(availability, "done", now)

            time.sleep(interval)

    except KeyboardInterrupt:
        click.echo("\nStopped watching.")
        sys.exit(0)
    finally:
        api_logger.setLevel(original_level)


def _format_availability_table(availability, workspace_mode: bool = False) -> None:
    title = "📊 GPU Availability (Workspace)" if workspace_mode else "📊 GPU Availability (Live)"
    scope_note = "Shows availability in your workspace only" if workspace_mode else ""

    lines = [
        "",
        title,
        "─" * 80,
    ]

    if scope_note:
        lines.append(f"{scope_note}")
        lines.append("─" * 80)

    lines.append(
        f"{'GPU Type':<12} {'Location':<25} {'Ready':<8} {'Free':<8} {'Free GPUs':<12}",
    )
    lines.append("─" * 80)

    for a in availability:
        location = a.group_name[:24]
        gpu_type = a.gpu_type[:11]

        free_gpus = a.free_gpus
        if free_gpus >= 8:
            status = ""
        elif free_gpus > 0:
            status = "⚠"
        else:
            status = "✗"

        lines.append(
            f"{gpu_type:<12} {location:<25} {a.ready_nodes:<8} {a.free_nodes:<8} "
            f"{free_gpus:<12} {status}"
        )

    lines.append("─" * 80)
    lines.append("")
    lines.append("💡 Usage:")
    lines.append('  inspire run "python train.py"              # Auto-select best group')
    lines.append('  inspire run "python train.py" --type H100   # Prefer H100')
    lines.append('  inspire run "python train.py" --gpus 4      # Use 4 GPUs')
    lines.append("")

    click.echo("\n".join(lines))


def _format_accurate_availability_table(availability) -> None:
    lines = [
        "",
        "📊 GPU Availability (Accurate Real-Time)",
        "─" * 95,
        (
            f"{'GPU Type':<22} {'Compute Group':<25} {'Available':>10} {'Used':>8} "
            f"{'Low Pri':>8} {'Total':>8}"
        ),
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
            f"{gpu_type:<22} {location:<25} {a.available_gpus:>10} {a.used_gpus:>8} "
            f"{a.low_priority_gpus:>8} {a.total_gpus:>8} {status}"
        )

        total_available += a.available_gpus
        total_used += a.used_gpus
        total_low_pri += a.low_priority_gpus
        total_gpus += a.total_gpus

    lines.append("─" * 95)
    lines.append(
        f"{'TOTAL':<22} {'':<25} {total_available:>10} {total_used:>8} {total_low_pri:>8} "
        f"{total_gpus:>8}"
    )
    lines.append("")
    lines.append("💡 Legend:")
    lines.append("  Available = GPUs ready to use (not running any tasks)")
    lines.append("  Used      = GPUs currently running tasks")
    lines.append("  Low Pri   = GPUs running low-priority tasks (can be preempted)")
    lines.append("")
    lines.append("💡 Usage:")
    lines.append('  inspire run "python train.py"              # Auto-select best group')
    lines.append('  inspire run "python train.py" --type H100   # Prefer H100')
    lines.append('  inspire run "python train.py" --gpus 4      # Use 4 GPUs')
    lines.append("")

    click.echo("\n".join(lines))
