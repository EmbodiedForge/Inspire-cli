"""Job commands for Inspire CLI.

Commands:
    inspire job create - Create a new training job
    inspire job status - Check job status
    inspire job stop   - Stop a running job
    inspire job wait   - Wait for job completion
    inspire job list   - List recent jobs from local cache
    inspire job logs   - View job logs
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_SUCCESS,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_VALIDATION_ERROR,
    EXIT_API_ERROR,
    EXIT_TIMEOUT,
    EXIT_LOG_NOT_FOUND,
    EXIT_JOB_NOT_FOUND,
)
from inspire.inspire_api_control import _validate_job_id_format
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.job_cache import JobCache
from inspire.cli.utils.github import (
    GitHubAuthError,
    GitHubError,
    fetch_remote_log_via_bridge,
)
from inspire.cli.formatters import json_formatter, human_formatter


@click.group()
def job():
    """Manage training jobs on the Inspire platform."""
    pass


@job.command("create")
@click.option("--name", "-n", required=True, help="Job name")
@click.option("--resource", "-r", required=True, help="Resource spec (e.g., '4xH200')")
@click.option("--command", "-c", required=True, help="Start command")
@click.option("--framework", default="pytorch", help="Training framework (default: pytorch)")
@click.option("--priority", type=int, default=lambda: int(os.environ.get("INSP_PRIORITY", "8")), help="Task priority 1-10 (default: 8, env: INSP_PRIORITY)")
@click.option("--max-time", type=float, default=100.0, help="Max runtime in hours (default: 100)")
@click.option("--location", help="Preferred datacenter location")
@click.option("--image", help="Custom Docker image")
@pass_context
def create(
    ctx: Context,
    name: str,
    resource: str,
    command: str,
    framework: str,
    priority: int,
    max_time: float,
    location: str,
    image: str,
):
    """Create a new training job.

    IMPORTANT: Always set INSPIRE_TARGET_LOG_DIR before running this command (from your laptop).
    This path should point to the shared filesystem on Bridge where training logs will be written
    (e.g., /train/logs).

    The command you provide will be wrapped to redirect stdout/stderr to this target directory:
      wrapped_command = (cd /training/code && bash train.sh) > /train/logs/job_name.log 2>&1

    When creating a job:
      - The wrapped command is sent to Inspire API
      - Inspire executes it on the Bridge machine
      - Logs are written to INSPIRE_TARGET_LOG_DIR on Bridge
      - log_path is cached in ~/.inspire/jobs.json for later retrieval

    When retrieving logs later:
      - Set INSPIRE_TARGET_LOG_DIR to the same path used during job creation
      - Use `inspire job logs <job_id>` to fetch logs via GitHub bridge

    \b
    Examples:
        export INSPIRE_TARGET_LOG_DIR="/train/logs"
        inspire job create --name "pr-123" --resource "4xH200" --command "cd /path/to/code && bash train.sh"
        inspire job create -n test -r H200 -c "python train.py" --priority 9
    """
    try:
        config = Config.from_env(require_target_dir=True)
        api = AuthManager.get_api(config)

        # If INSP_TARGET_DIR is configured, wrap the command so
        # stdout/stderr land in a single master log file under
        # ${INSP_TARGET_DIR}/.inspire/.
        final_command = command
        log_path = None
        if config.target_dir:
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            log_dir = os.path.join(config.target_dir, ".inspire")
            log_filename = f"training_master_{timestamp}.log"
            log_path = os.path.join(log_dir, log_filename)
            final_command = f'mkdir -p "{log_dir}" && ( {command} ) > "{log_path}" 2>&1'

        # Convert hours to milliseconds
        max_time_ms = str(int(max_time * 3600 * 1000))

        # Create job
        result = api.create_training_job_smart(
            name=name,
            command=final_command,
            resource=resource,
            framework=framework,
            prefer_location=location,
            image=image,
            task_priority=priority,
            max_running_time_ms=max_time_ms,
        )

        # Extract job ID from response
        data = result.get("data", {}) if isinstance(result, dict) else {}
        job_id = data.get("job_id")

        if job_id:
            # Save to local cache
            cache = JobCache(config.get_expanded_cache_path())
            cache.add_job(
                job_id=job_id,
                name=name,
                resource=resource,
                command=command,
                status="PENDING",
                log_path=log_path,
            )

        # Output
        if ctx.json_output:
            payload = data if data else result
            click.echo(json_formatter.format_json(payload))
        else:
            if job_id:
                click.echo(human_formatter.format_success(f"Job created: {job_id}"))
                click.echo(f"\nName:     {name}")
                click.echo(f"Resource: {resource}")
                max_cmd_len = 80
                if len(command) > max_cmd_len:
                    display_cmd = command[:max_cmd_len]
                    suffix = " ... (truncated)"
                else:
                    display_cmd = command
                    suffix = ""
                click.echo(f"Command:  {display_cmd}{suffix}")
                if log_path:
                    click.echo(f"Log file:  {log_path}")
                click.echo(f"\nCheck status with: inspire job status {job_id}")
            else:
                if isinstance(result, dict):
                    message = result.get("message") or "Job created (no job ID returned)"
                    click.echo(human_formatter.format_success(message))
                    if result.get("data"):
                        click.echo(str(result["data"]))
                else:
                    click.echo(human_formatter.format_success("Job created (no job ID returned)"))
                    click.echo(str(result))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@job.command("status")
@click.argument("job_id")
@pass_context
def status(ctx: Context, job_id: str):
    """Check the status of a training job.

    \b
    Example:
        inspire job status job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf
    """
    # Validate job ID format early (before auth/API calls)
    format_error = _validate_job_id_format(job_id)
    if format_error:
        _handle_error(ctx, "InvalidJobID", format_error, EXIT_JOB_NOT_FOUND)
        return

    try:
        config = Config.from_env()
        api = AuthManager.get_api(config)

        result = api.get_job_detail(job_id)
        job_data = result.get("data", {})

        # Update local cache
        if job_data.get("status"):
            cache = JobCache(config.get_expanded_cache_path())
            cache.update_status(job_id, job_data["status"])

        # Output
        if ctx.json_output:
            click.echo(json_formatter.format_json(job_data))
        else:
            click.echo(human_formatter.format_job_status(job_data))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        if "not found" in str(e).lower() or "invalid job id" in str(e).lower():
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@job.command("stop")
@click.argument("job_id")
@click.option("--force", "-f", is_flag=True, help="Skip confirmation")
@pass_context
def stop(ctx: Context, job_id: str, force: bool):
    """Stop a running training job.

    \b
    Example:
        inspire job stop job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf
        inspire job stop job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --force
    """
    # Validate job ID format early (before auth/API calls)
    format_error = _validate_job_id_format(job_id)
    if format_error:
        _handle_error(ctx, "InvalidJobID", format_error, EXIT_JOB_NOT_FOUND)
        return

    if not force:
        click.confirm(f"Stop job {job_id}?", abort=True)

    try:
        config = Config.from_env()
        api = AuthManager.get_api(config)

        api.stop_training_job(job_id)

        # Update local cache
        cache = JobCache(config.get_expanded_cache_path())
        cache.update_status(job_id, "CANCELLED")

        # Output
        if ctx.json_output:
            click.echo(json_formatter.format_json({"job_id": job_id, "status": "stopped"}))
        else:
            click.echo(human_formatter.format_success(f"Job stopped: {job_id}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        if "not found" in str(e).lower() or "invalid job id" in str(e).lower():
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@job.command("wait")
@click.argument("job_id")
@click.option("--timeout", type=int, default=14400, help="Timeout in seconds (default: 4 hours)")
@click.option("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
@pass_context
def wait(ctx: Context, job_id: str, timeout: int, interval: int):
    """Wait for a job to complete.

    Polls the job status until it reaches a terminal state
    (SUCCEEDED, FAILED, or CANCELLED).

    \b
    Example:
        inspire job wait job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --timeout 7200
    """
    # Validate job ID format early (before auth/API calls)
    format_error = _validate_job_id_format(job_id)
    if format_error:
        _handle_error(ctx, "InvalidJobID", format_error, EXIT_JOB_NOT_FOUND)
        return

    try:
        config = Config.from_env()
        api = AuthManager.get_api(config)
        cache = JobCache(config.get_expanded_cache_path())

        terminal_statuses = {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",  # Uppercase
            "job_succeeded",
            "job_failed",
            "job_cancelled",  # API snake_case
        }
        start_time = time.time()
        last_status = None

        click.echo(f"Waiting for job {job_id} (timeout: {timeout}s, interval: {interval}s)")

        while True:
            elapsed = time.time() - start_time

            if elapsed > timeout:
                if ctx.json_output:
                    click.echo(
                        json_formatter.format_json_error(
                            "Timeout", f"Timeout after {timeout}s", EXIT_TIMEOUT
                        )
                    )
                else:
                    click.echo(human_formatter.format_error(f"Timeout after {timeout}s"))
                sys.exit(EXIT_TIMEOUT)

            try:
                result = api.get_job_detail(job_id)
                job_data = result.get("data", {})
                current_status = job_data.get("status", "UNKNOWN")

                # Update cache
                cache.update_status(job_id, current_status)

                # Print status change or progress
                if current_status != last_status:
                    if ctx.json_output:
                        click.echo(
                            json_formatter.format_json(
                                {
                                    "event": "status_change",
                                    "status": current_status,
                                    "elapsed_seconds": int(elapsed),
                                }
                            )
                        )
                    else:
                        emoji = human_formatter.STATUS_EMOJI.get(current_status, "\U0001f4ca")
                        click.echo(f"\n{emoji} Status: {current_status}")
                    last_status = current_status
                else:
                    if not ctx.json_output:
                        # Progress indicator
                        mins = int(elapsed // 60)
                        secs = int(elapsed % 60)
                        click.echo(
                            f"\r[{mins:02d}:{secs:02d}] Waiting... Status: {current_status}",
                            nl=False,
                        )

                # Check if done
                if current_status in terminal_statuses:
                    if ctx.json_output:
                        click.echo(json_formatter.format_json(job_data))
                    else:
                        click.echo("")  # Newline after progress
                        click.echo(human_formatter.format_job_status(job_data))

                    # Exit with appropriate code
                    if current_status in {"SUCCEEDED", "job_succeeded"}:
                        sys.exit(EXIT_SUCCESS)
                    else:
                        sys.exit(EXIT_GENERAL_ERROR)

            except Exception as e:
                if not ctx.json_output:
                    click.echo(f"\nWarning: Failed to get status: {e}")

            time.sleep(interval)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except KeyboardInterrupt:
        click.echo("\nInterrupted")
        sys.exit(EXIT_GENERAL_ERROR)


@job.command("list")
@click.option("--limit", "-n", type=int, default=10, help="Max jobs to show (default: 10)")
@click.option("--status", "-s", help="Filter by status (PENDING, RUNNING, SUCCEEDED, FAILED)")
@pass_context
def list_jobs(ctx: Context, limit: int, status: str):
    """List recent jobs from local cache.

    Note: This lists jobs from the local cache, not from the API
    (the API doesn't have a list endpoint).

    \b
    Example:
        inspire job list
        inspire job list --limit 20 --status RUNNING
    """
    try:
        config = Config.from_env()
        cache = JobCache(config.get_expanded_cache_path())

        jobs = cache.list_jobs(limit=limit, status=status)

        if ctx.json_output:
            click.echo(json_formatter.format_json(jobs))
        else:
            click.echo(human_formatter.format_job_list(jobs))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


@job.command("update")
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
def update_jobs(ctx: Context, status: tuple, limit: int, delay: float):
    """Update cached jobs by polling the API.

    Refreshes statuses for cached jobs matching the status filter
    (defaults to PENDING/RUNNING/QUEUING and API snake_case aliases) and
    updates the local cache. Skips jobs that fail to refresh and
    reports them.
    """
    # Build status set with aliases
    default_statuses = ("PENDING", "RUNNING", "QUEUING") if not status else tuple(status)
    alias_map = {
        "PENDING": {"PENDING", "job_pending"},
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
        cache = JobCache(config.get_expanded_cache_path())

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
                time.sleep(delay)

        if ctx.json_output:
            payload = {
                "updated": updated,
                "errors": errors,
            }
            click.echo(json_formatter.format_json(payload))
        else:
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


@job.command("logs")
@click.argument("job_id", required=False)
@click.option("--tail", "-n", type=int, help="Show last N lines only")
@click.option("--path", is_flag=True, help="Just print log path, don't read content")
@click.option("--refresh", is_flag=True, help="Re-fetch log even if a cached copy exists")
@click.option("--update", is_flag=True, help="Fetch and cache logs for cached jobs in bulk.")
@click.option(
    "--status",
    "-s",
    multiple=True,
    help="Status filter when using --update (e.g., RUNNING). Repeatable.",
)
@click.option(
    "--limit",
    "-m",
    type=int,
    default=0,
    help="Max cached jobs to process with --update (0 = all).",
)
@pass_context
def logs(
    ctx: Context,
    job_id: Optional[str],
    tail: int,
    path: bool,
    refresh: bool,
    update: bool,
    status: tuple,
    limit: int,
):
    """View logs for a training job.

    Fetches logs via GitHub Actions bridge and caches them locally. Use
    --update to pull logs for many cached jobs in one go.

    \b
    Examples:
        inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf
        inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --tail 100
        inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --path
        inspire job logs job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --refresh
        inspire job logs --update --status RUNNING --status SUCCEEDED
    """
    if update:
        if tail or path:
            _handle_error(
                ctx,
                "InvalidUsage",
                "--update cannot be combined with --tail/--path",
                EXIT_VALIDATION_ERROR,
            )
        _bulk_update_logs(ctx, status=status, limit=limit, refresh=refresh, job_id=job_id)
        return

    if not job_id:
        _handle_error(
            ctx,
            "InvalidUsage",
            "Job ID is required unless using --update",
            EXIT_VALIDATION_ERROR,
        )

    # Validate job ID format early
    format_error = _validate_job_id_format(job_id)
    if format_error:
        _handle_error(ctx, "InvalidJobID", format_error, EXIT_JOB_NOT_FOUND)
        return

    try:
        config = Config.from_env(require_target_dir=False)
        cache = JobCache(config.get_expanded_cache_path())

        # Resolve job from cache
        cached = cache.get_job(job_id)
        if not cached:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "JobNotFound",
                        f"Job not found: {job_id}",
                        EXIT_JOB_NOT_FOUND,
                    )
                )
            else:
                click.echo(human_formatter.format_error(f"Job not found: {job_id}"))
            sys.exit(EXIT_JOB_NOT_FOUND)

        remote_log_path_str = cached.get("log_path")
        if not remote_log_path_str:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "LogNotFound",
                        f"No log file found for job {job_id}",
                        EXIT_LOG_NOT_FOUND,
                    )
                )
            else:
                click.echo(human_formatter.format_error(f"No log file found for job {job_id}"))
            sys.exit(EXIT_LOG_NOT_FOUND)

        # Compute cache path for this job.
        cache_dir = Path(os.path.expanduser(config.log_cache_dir))
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{job_id}.log"
        legacy_cache_path = cache_dir / f"job-{job_id}.log"

        # Migrate legacy filename if present
        if not cache_path.exists() and legacy_cache_path.exists():
            try:
                legacy_cache_path.replace(cache_path)
            except OSError:
                cache_path = legacy_cache_path

        # Let the user know when we are about to use the remote
        # GitHub Actions bridge path, since the first fetch typically
        # takes a few seconds while the workflow runs and the
        # artifact is prepared.
        needs_remote_fetch = refresh or not cache_path.exists()
        if needs_remote_fetch and not ctx.json_output:
            click.echo(
                "⏳ Fetching remote log via GitHub Actions bridge (first fetch may take ~10–30s)..."
            )

        if needs_remote_fetch:
            try:
                fetch_remote_log_via_bridge(
                    config=config,
                    job_id=job_id,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    refresh=refresh,
                )
            except GitHubAuthError as e:
                _handle_error(
                    ctx,
                    "ConfigError",
                    str(e),
                    EXIT_CONFIG_ERROR,
                )
            except TimeoutError as e:
                _handle_error(
                    ctx,
                    "Timeout",
                    str(e),
                    EXIT_TIMEOUT,
                )
            except GitHubError as e:
                error_msg = (
                    f"{str(e)}\n\n"
                    f"Hints:\n"
                    f"- Check that the training job created a log file at: {remote_log_path_str}\n"
                    f"- Verify the Bridge workflow exists and can access the shared filesystem\n"
                    f"- View GitHub Actions runs at: https://github.com/{config.github_repo}/actions"
                )
                _handle_error(
                    ctx,
                    "RemoteLogError",
                    error_msg,
                    EXIT_GENERAL_ERROR,
                )

        if not cache_path.exists():
            _handle_error(
                ctx,
                "LogNotFound",
                f"Failed to retrieve log for job {job_id}; the Bridge workflow may have failed.",
                EXIT_LOG_NOT_FOUND,
            )

        # Print path only
        if path:
            if ctx.json_output:
                click.echo(json_formatter.format_json({"log_path": str(cache_path)}))
            else:
                click.echo(str(cache_path))
            return

        # Print tail
        if tail:
            try:
                with cache_path.open("r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
                tail_lines = lines[-tail:] if tail > 0 else lines
                if ctx.json_output:
                    click.echo(
                        json_formatter.format_json(
                            {
                                "log_path": str(cache_path),
                                "lines": tail_lines,
                                "count": len(tail_lines),
                            }
                        )
                    )
                else:
                    click.echo(f"=== Last {len(tail_lines)} lines ===\n")
                    for line in tail_lines:
                        click.echo(line)
            except OSError as e:
                _handle_error(ctx, "LogNotFound", str(e), EXIT_LOG_NOT_FOUND)
            return

        # Default: print full file
        try:
            content = cache_path.read_text(encoding="utf-8", errors="replace")
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "log_path": str(cache_path),
                            "content": content,
                            "size_bytes": len(content),
                        }
                    )
                )
            else:
                click.echo(content)
        except OSError as e:
            _handle_error(ctx, "LogNotFound", str(e), EXIT_LOG_NOT_FOUND)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


def _bulk_update_logs(
    ctx: Context,
    status: tuple,
    limit: int,
    refresh: bool,
    job_id: Optional[str] = None,
) -> None:
    """Fetch and cache logs for many jobs from the local cache."""
    try:
        config = Config.from_env(require_target_dir=False)
        cache = JobCache(config.get_expanded_cache_path())

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

        if job_id:
            format_error = _validate_job_id_format(job_id)
            if format_error:
                _handle_error(ctx, "InvalidJobID", format_error, EXIT_JOB_NOT_FOUND)
            job = cache.get_job(job_id)
            if not job:
                _handle_error(ctx, "JobNotFound", f"Job not found: {job_id}", EXIT_JOB_NOT_FOUND)
            jobs = [job] if not status_filter or job.get("status") in status_filter else []
        else:
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
                fetch_remote_log_via_bridge(
                    config=config,
                    job_id=job_id_item,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    refresh=refresh,
                )
                updated.append({"job_id": job_id_item, "log_path": str(cache_path)})
            except GitHubAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                errors.append({"job_id": job_id_item, "error": str(e)})
            except GitHubError as e:
                error_msg = (
                    f"{str(e)}\n\n"
                    f"Hints:\n"
                    f"- Check that the training job created a log file at: {remote_log_path_str}\n"
                    f"- Verify the Bridge workflow exists and can access the shared filesystem\n"
                    f"- View GitHub Actions runs at: https://github.com/{config.github_repo}/actions"
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
            "job_filter": job_id,
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
        target_label = f" for job {job_id}" if job_id else ""
        click.echo(
            f"Updating logs for {total_candidates} cached job(s){status_label}{target_label} (refresh={refresh})"
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


def _handle_error(ctx: Context, error_type: str, message: str, exit_code: int):
    """Handle and format errors consistently."""
    if ctx.json_output:
        click.echo(json_formatter.format_json_error(error_type, message, exit_code), err=True)
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(exit_code)
