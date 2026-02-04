"""Bulk log fetching for `inspire job logs`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR
from inspire.cli.formatters import json_formatter
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.gitea import GiteaAuthError, GiteaError


def _bulk_update_logs(
    ctx: Context,
    status: tuple,
    limit: int,
    refresh: bool,
    *,
    deps,
) -> None:
    """Fetch and cache logs for many jobs from the local cache."""
    try:
        config = Config.from_env(require_target_dir=False)
        cache = deps.JobCache(config.get_expanded_cache_path())

        alias_map = {
            "PENDING": {"PENDING", "job_pending"},
            "RUNNING": {"RUNNING", "job_running"},
            "SUCCEEDED": {"SUCCEEDED", "job_succeeded"},
            "FAILED": {"FAILED", "job_failed"},
            "CANCELLED": {"CANCELLED", "job_cancelled"},
        }

        status_filter = set()
        if status:
            for s in status:
                key = str(s).upper()
                status_filter.update(alias_map.get(key, {s}))

        jobs = cache.list_jobs(limit=limit)
        if status_filter:
            jobs = [j for j in jobs if j.get("status") in status_filter]

        total_candidates = len(jobs)

        cache_dir = Path(os.path.expanduser(config.log_cache_dir))
        cache_dir.mkdir(parents=True, exist_ok=True)

        updated = []
        errors = []
        skipped_no_log = []

        for job in jobs:
            job_id_item = job.get("job_id")
            remote_log_path_str = job.get("log_path")

            if not job_id_item:
                continue

            if not remote_log_path_str:
                skipped_no_log.append(job_id_item)
                continue

            cache_path = cache_dir / f"{job_id_item}.log"

            try:
                deps.fetch_remote_log_via_bridge(
                    config=config,
                    job_id=job_id_item,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    refresh=refresh,
                )
                updated.append({"job_id": job_id_item, "log_path": str(cache_path)})
            except GiteaAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                errors.append({"job_id": job_id_item, "error": str(e)})
            except GiteaError as e:
                error_msg = (
                    f"{str(e)}\n\n"
                    f"Hints:\n"
                    f"- Check that the training job created a log file at: {remote_log_path_str}\n"
                    f"- Verify the Bridge workflow exists and can access the shared filesystem\n"
                    f"- View Gitea Actions at: {config.gitea_server}/{config.gitea_repo}/actions"
                )
                errors.append({"job_id": job_id_item, "error": error_msg})
            except Exception as e:  # noqa: BLE001
                errors.append({"job_id": job_id_item, "error": str(e)})

        success_flag = not errors

        payload = {
            "updated": updated,
            "errors": errors,
            "skipped_no_log_path": skipped_no_log,
            "processed": total_candidates,
            "fetched": len(updated),
            "refresh": refresh,
            "status_filter": sorted(status_filter),
            "limit": limit,
        }

        if ctx.json_output:
            click.echo(json_formatter.format_json(payload, success=success_flag))
            if not success_flag:
                sys.exit(EXIT_GENERAL_ERROR)
            return

        if not jobs:
            click.echo("No cached jobs matched the filter.")
            return

        status_label = f" with status in {sorted(status_filter)}" if status_filter else ""
        click.echo(
            f"Updating logs for {total_candidates} cached job(s){status_label} (refresh={refresh})"
        )

        if updated:
            click.echo("\nFetched:")
            for entry in updated:
                click.echo(f"- {entry['job_id']}: {entry['log_path']}")

        if skipped_no_log:
            click.echo("\nSkipped (no log_path in cache): " + ", ".join(skipped_no_log))

        if errors:
            click.echo("\nErrors:")
            for err in errors:
                click.echo(f"- {err['job_id']}: {err['error']}")
            sys.exit(EXIT_GENERAL_ERROR)

        click.echo("\nDone.")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
