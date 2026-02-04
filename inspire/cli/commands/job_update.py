"""Job update command."""

from __future__ import annotations

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error


def build_update_command(deps) -> click.Command:
    @click.command("update")
    @click.option(
        "--status",
        "-s",
        multiple=True,
        help="Status filter (default: PENDING,RUNNING + API aliases). Repeatable.",
    )
    @click.option(
        "--limit",
        "-n",
        type=int,
        default=10,
        help="Max jobs to refresh from cache (default: 10)",
    )
    @click.option(
        "--delay",
        "-d",
        type=float,
        default=0.6,
        help="Delay between API requests in seconds to avoid rate limits (default: 0.6)",
    )
    @pass_context
    def update_jobs(ctx: Context, status: tuple, limit: int, delay: float) -> None:
        """Update cached jobs by polling the API.

        Refreshes statuses for cached jobs matching the status filter
        (defaults to PENDING/RUNNING/QUEUING and API snake_case aliases) and
        updates the local cache. Skips jobs that fail to refresh and
        reports them.
        """
        # Build status set with aliases
        default_statuses = ("PENDING", "RUNNING", "QUEUING") if not status else tuple(status)
        alias_map = {
            # Some API backends return early-stage states like "job_creating".
            # Treat them as PENDING so `job update` keeps refreshing them by default.
            "PENDING": {"PENDING", "job_pending", "job_creating"},
            "RUNNING": {"RUNNING", "job_running"},
            "QUEUING": {"QUEUING", "job_queuing"},
            "SUCCEEDED": {"SUCCEEDED", "job_succeeded"},
            "FAILED": {"FAILED", "job_failed"},
            "CANCELLED": {"CANCELLED", "job_cancelled"},
        }
        statuses_set = set()
        for s in default_statuses:
            key = str(s).upper()
            statuses_set.update(alias_map.get(key, {s}))

        try:
            config = Config.from_env()
            api = AuthManager.get_api(config)
            cache = deps.JobCache(config.get_expanded_cache_path())

            # Fetch from cache then filter in-memory to support multiple statuses/aliases
            jobs = cache.list_jobs(limit=limit)
            jobs = [j for j in jobs if j.get("status") in statuses_set]

            updated = []
            errors = []

            for job in jobs:
                job_id = job.get("job_id")
                if not job_id:
                    continue
                old_status = job.get("status", "UNKNOWN")
                try:
                    result = api.get_job_detail(job_id)
                    data = result.get("data", {}) if isinstance(result, dict) else {}
                    new_status = data.get("status") or data.get("job_status") or old_status
                    if new_status:
                        cache.update_status(job_id, new_status)
                    updated.append(
                        {
                            "job_id": job_id,
                            "old_status": old_status,
                            "new_status": new_status,
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    errors.append({"job_id": job_id, "error": str(e)})
                if delay > 0:
                    deps.time.sleep(delay)

            if ctx.json_output:
                payload = {
                    "updated": updated,
                    "errors": errors,
                }
                click.echo(json_formatter.format_json(payload))
                return

            # Show updated list (only those processed)
            if updated:
                # Re-read to display latest statuses
                refreshed_jobs = [cache.get_job(u["job_id"]) for u in updated]
                refreshed_jobs = [j for j in refreshed_jobs if j]
                click.echo(human_formatter.format_job_list(refreshed_jobs))
            else:
                click.echo("\nNo matching jobs to update.\n")

            if errors:
                click.echo("\nErrors during update:")
                for err in errors:
                    click.echo(f"- {err['job_id']}: {err['error']}")

        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        except AuthenticationError as e:
            _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        except Exception as e:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)

    return update_jobs
