"""Job logs command."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.commands.job_logs_flow_single import run_job_logs_single_job
from inspire.cli.commands.job_logs_helpers import _bulk_update_logs
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.job_cli import ensure_valid_job_id


def build_logs_command(deps) -> click.Command:
    @click.command("logs")
    @click.argument("job_id", required=False)
    @click.option("--tail", "-n", type=int, help="Show last N lines only")
    @click.option("--head", type=int, help="Show first N lines only")
    @click.option("--path", is_flag=True, help="Just print log path, don't read content")
    @click.option(
        "--refresh", is_flag=True, help="Re-fetch log from the beginning (ignore cached offset)"
    )
    @click.option("--follow", "-f", is_flag=True, help="Continuously poll for new log content")
    @click.option(
        "--interval",
        type=int,
        default=30,
        help="Poll interval for --follow in seconds (default: 30)",
    )
    @click.option(
        "--status",
        "-s",
        multiple=True,
        help="Status filter for bulk mode (e.g., RUNNING). Repeatable.",
    )
    @click.option(
        "--limit",
        "-m",
        type=int,
        default=0,
        help="Max cached jobs to process in bulk mode (0 = all).",
    )
    @pass_context
    def logs(
        ctx: Context,
        job_id: Optional[str],
        tail: int,
        head: int,
        path: bool,
        refresh: bool,
        follow: bool,
        interval: int,
        status: tuple,
        limit: int,
    ) -> None:
        """View logs for a training job.

        Fetches logs via Gitea workflow and caches them locally.
        Incremental fetching is enabled by default - only new bytes are
        fetched when a local cache exists. Use --refresh to re-fetch from
        the beginning.

        \b
        Single job mode (with JOB_ID):
            Fetches and displays the log for a specific job.

        Bulk mode (without JOB_ID):
            Fetches and caches logs for multiple jobs from local cache.
            Use --status to filter by job status.

        \b
        Examples:
            inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf
            inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --tail 100
            inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --head 50
            inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --follow
            inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --follow --interval 10
            inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --path
            inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --refresh
            inspire job logs --status RUNNING --status SUCCEEDED
            inspire job logs --refresh --status RUNNING
        """
        # Bulk mode: no job_id provided
        if not job_id:
            if tail or head or path or follow:
                _handle_error(
                    ctx,
                    "InvalidUsage",
                    "--tail, --head, --path and --follow require a JOB_ID",
                    EXIT_VALIDATION_ERROR,
                )
                return
            _bulk_update_logs(ctx, status=status, limit=limit, refresh=refresh, deps=deps)
            return

        if not ensure_valid_job_id(ctx, job_id):
            return

        run_job_logs_single_job(
            ctx,
            job_id=job_id,
            tail=tail,
            head=head,
            path=path,
            refresh=refresh,
            follow=follow,
            interval=interval,
            deps=deps,
        )

    return logs
