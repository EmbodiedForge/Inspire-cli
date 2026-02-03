"""Watch mode for `inspire resources list`."""

from __future__ import annotations

import logging
import os
import sys
import time

import click

from inspire.cli.commands.resources_list_known_groups import _known_compute_groups_from_config
from inspire.cli.context import Context, EXIT_AUTH_ERROR
from inspire.cli.formatters import human_formatter
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.config import Config
from inspire.cli.utils.resources import clear_availability_cache, fetch_resource_availability
from inspire.cli.utils.web_session import SessionExpiredError, get_web_session


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
