"""Watch mode flow for `inspire resources list`."""

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

from .watch_render import render_display


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

    progress_state = {"fetched": 0, "total": 0}

    def on_progress(fetched: int, total: int) -> None:
        if mode != "nodes":
            return
        progress_state["fetched"] = fetched
        progress_state["total"] = total
        now = datetime.now().strftime("%H:%M:%S")
        render_display(
            mode=mode,
            availability=availability,
            phase="fetching",
            timestamp=now,
            interval=interval,
            progress_state=progress_state,
        )

    try:
        availability: list = []
        while True:
            progress_state["fetched"] = 0
            progress_state["total"] = 0

            now = datetime.now().strftime("%H:%M:%S")
            render_display(
                mode=mode,
                availability=availability,
                phase="fetching",
                timestamp=now,
                interval=interval,
                progress_state=progress_state,
            )

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
            render_display(
                mode=mode,
                availability=availability,
                phase="done",
                timestamp=now,
                interval=interval,
                progress_state=progress_state,
            )

            time.sleep(interval)

    except KeyboardInterrupt:
        click.echo("\nStopped watching.")
        sys.exit(0)
    finally:
        api_logger.setLevel(original_level)


__all__ = ["_watch_resources"]
