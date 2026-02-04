"""Job list command."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.commands.job_list_watch import _watch_jobs
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error


def build_list_command(deps) -> click.Command:
    @click.command("list")
    @click.option("--limit", "-n", type=int, default=10, help="Max jobs to show (default: 10)")
    @click.option("--status", "-s", help="Filter by status (PENDING, RUNNING, SUCCEEDED, FAILED)")
    @click.option(
        "--active",
        "-a",
        is_flag=True,
        help="Show only active jobs (exclude failed, cancelled, stopped)",
    )
    @click.option("--watch", "-w", is_flag=True, help="Continuously refresh job list")
    @click.option(
        "--interval",
        type=int,
        default=10,
        help="Refresh interval in seconds for --watch (default: 10)",
    )
    @pass_context
    def list_jobs(
        ctx: Context,
        limit: int,
        status: Optional[str],
        active: bool,
        watch: bool,
        interval: int,
    ) -> None:
        """List recent jobs from local cache.

        Note: This lists jobs from the local cache, not from the API
        (the API doesn't have a list endpoint).

        \b
        Example:
            inspire job list
            inspire job list --limit 20 --status RUNNING
            inspire job list --active
            inspire job list --watch --active -n 20
            inspire job list --watch --interval 5
        """
        try:
            config = Config.from_env()

            # Handle watch mode
            if watch:
                _watch_jobs(
                    ctx=ctx,
                    config=config,
                    limit=limit,
                    status=status,
                    active=active,
                    interval=interval,
                    deps=deps,
                )
                return

            cache = deps.JobCache(config.get_expanded_cache_path())

            # Define statuses to exclude when --active flag is set
            exclude_statuses = None
            if active:
                exclude_statuses = {
                    "FAILED",
                    "job_failed",
                    "CANCELLED",
                    "job_cancelled",
                    "job_stopped",
                }

            jobs = cache.list_jobs(limit=limit, status=status, exclude_statuses=exclude_statuses)

            if ctx.json_output:
                click.echo(json_formatter.format_json(jobs))
            else:
                click.echo(human_formatter.format_job_list(jobs))

        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        except Exception as e:
            _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)

    return list_jobs
