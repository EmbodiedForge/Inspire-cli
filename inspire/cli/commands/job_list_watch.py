"""Watch mode for `inspire job list`."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Optional

import click

from inspire.cli.context import Context, EXIT_AUTH_ERROR, EXIT_SUCCESS
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.config import Config
from inspire.cli.utils.errors import exit_with_error as _handle_error


def _watch_jobs(
    ctx: Context,
    config: Config,
    limit: int,
    status: Optional[str],
    active: bool,
    interval: int,
    *,
    deps,
) -> None:
    """Continuously poll and display job status with incremental updates."""
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    cache = deps.JobCache(config.get_expanded_cache_path())

    if not ctx.json_output:
        click.echo("🔐 Authenticating...")

    try:
        api = AuthManager.get_api(config)
    except AuthenticationError as e:
        api_logger.setLevel(original_level)
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        return

    exclude_statuses = None
    if active:
        exclude_statuses = {"FAILED", "job_failed", "CANCELLED", "job_cancelled", "job_stopped"}

    terminal_statuses = {
        "SUCCEEDED",
        "job_succeeded",
        "FAILED",
        "job_failed",
        "CANCELLED",
        "job_cancelled",
        "job_stopped",
    }

    completed_this_session: list = []
    completed_job_ids: set = set()

    def _progress_bar(current: int, total: int, width: int = 20) -> str:
        if total == 0:
            return "░" * width
        filled = int(width * current / total)
        return "█" * filled + "░" * (width - filled)

    def _render_display(
        jobs_list: list,
        updated_count: int,
        total_count: int,
        completed_list: list,
    ) -> None:
        os.system("clear")
        if ctx.json_output:
            timestamp = datetime.now().strftime("%H:%M:%S")
            click.echo(
                json_formatter.format_json(
                    {
                        "event": "refresh",
                        "timestamp": timestamp,
                        "updated": updated_count,
                        "total": total_count,
                        "jobs": jobs_list,
                        "completed_this_session": completed_list,
                    }
                )
            )
        else:
            bar = _progress_bar(updated_count, total_count)
            if updated_count < total_count:
                click.echo(f"🔄 [{bar}] {updated_count}/{total_count} updating...\n")
            else:
                click.echo(f"✅ [{bar}] {total_count}/{total_count} done (interval: {interval}s)\n")

            click.echo(human_formatter.format_job_list(jobs_list))

            if completed_list:
                click.echo(f"\n✅ Completed This Session ({len(completed_list)})")
                click.echo("─" * 60)
                for job_item in completed_list:
                    status_emoji = (
                        "✅" if "succeeded" in job_item.get("status", "").lower() else "❌"
                    )
                    click.echo(
                        f"{job_item.get('job_id', 'N/A')[:36]:36}  "
                        f"{job_item.get('name', 'N/A')[:20]:20}  "
                        f"{status_emoji} {job_item.get('status', 'N/A')}"
                    )

    try:
        while True:
            jobs = cache.list_jobs(limit=limit, status=status, exclude_statuses=exclude_statuses)
            total = len(jobs)

            _render_display(jobs, 0, total, completed_this_session)

            for i, job_item in enumerate(jobs):
                job_id = job_item.get("job_id")
                if job_id:
                    original_status = job_item.get("status", "")
                    try:
                        result = api.get_job_detail(job_id)
                        data = result.get("data", {})
                        new_status = data.get("status")
                        if new_status:
                            job_item["status"] = new_status
                            cache.update_status(job_id, new_status)

                            if (
                                new_status in terminal_statuses
                                and original_status not in terminal_statuses
                                and job_id not in completed_job_ids
                            ):
                                completed_this_session.append(dict(job_item))
                                completed_job_ids.add(job_id)
                    except Exception:
                        pass

                _render_display(jobs, i + 1, total, completed_this_session)

                if i < total - 1:
                    deps.time.sleep(1.0)

            if active and exclude_statuses:
                filtered = [j for j in jobs if j.get("status") not in exclude_statuses]
                if len(filtered) != len(jobs):
                    _render_display(filtered, total, total, completed_this_session)

            deps.time.sleep(interval)

    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nStopped watching.")
        sys.exit(EXIT_SUCCESS)
    finally:
        api_logger.setLevel(original_level)


__all__ = ["_watch_jobs"]
