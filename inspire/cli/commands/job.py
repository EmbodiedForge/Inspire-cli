"""Job commands for Inspire CLI.

Commands:
    inspire job create - Create a new training job
    inspire job status - Check job status
    inspire job command - Show job start command
    inspire job stop   - Stop a running job
    inspire job wait   - Wait for job completion
    inspire job list   - List recent jobs from local cache
    inspire job logs   - View job logs
"""

import logging
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
from inspire.cli.utils.config import Config, ConfigError, build_env_exports
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.job_cache import JobCache
from inspire.cli.utils.gitea import (
    GiteaAuthError,
    GiteaError,
    fetch_remote_log_via_bridge,
    fetch_remote_log_incremental,
)
from inspire.cli.utils.tunnel import (
    is_tunnel_available,
    run_ssh_command,
    TunnelNotAvailableError,
)
from inspire.cli.utils.browser_api import find_best_compute_group_accurate
from inspire.cli.formatters import json_formatter, human_formatter


@click.group()
def job():
    """Manage training jobs on the Inspire platform."""
    pass


def _wrap_in_bash(command: str) -> str:
    """Wrap command in bash -c if not already wrapped.

    The Inspire platform's remote shell is sh (dash), which doesn't support
    bash features like 'source'. This wraps all commands in bash to ensure
    consistent behavior.
    """
    stripped = command.strip()

    # Skip if already wrapped
    if stripped.startswith(("bash -c ", "sh -c ", "/bin/bash -c ", "/bin/sh -c ")):
        return command

    # Escape single quotes: ' -> '\''
    escaped = command.replace("'", "'\\''")
    return f"bash -c '{escaped}'"


@job.command("create")
@click.option("--name", "-n", required=True, help="Job name")
@click.option("--resource", "-r", required=True, help="Resource spec (e.g., '4xH200')")
@click.option("--command", "-c", required=True, help="Start command")
@click.option("--framework", default="pytorch", help="Training framework (default: pytorch)")
@click.option("--priority", type=int, default=lambda: int(os.environ.get("INSP_PRIORITY", "6")), help="Task priority 1-10 (default: 6, env: INSP_PRIORITY)")
@click.option("--max-time", type=float, default=100.0, help="Max runtime in hours (default: 100)")
@click.option("--location", help="Preferred datacenter location")
@click.option(
    "--auto/--no-auto",
    default=True,
    help="Auto-select best location based on node availability (default: auto)",
)
@click.option("--image", default=lambda: os.environ.get("INSP_IMAGE"), help="Custom Docker image")
@click.option(
    "--nodes",
    type=int,
    default=1,
    help="Number of nodes for multi-node training (default: 1)",
)
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
    auto: bool,
    image: str,
    nodes: int,
):
    """Create a new training job.

    IMPORTANT: Always set INSPIRE_TARGET_DIR before running this command (from your laptop).
    This path should point to the shared filesystem on Bridge where training logs will be written
    (e.g., /train/logs).

    The command you provide will be wrapped to redirect stdout/stderr to this target directory:
      wrapped_command = (cd /training/code && bash train.sh) > /train/logs/job_name.log 2>&1

    When creating a job:
      - The wrapped command is sent to Inspire API
      - Inspire executes it on the Bridge machine
      - Logs are written to INSPIRE_TARGET_DIR on Bridge
      - log_path is cached in ~/.inspire/jobs.json for later retrieval

    When retrieving logs later:
      - Set INSPIRE_TARGET_DIR to the same path used during job creation
      - Use `inspire job logs <job_id>` to fetch logs via Gitea bridge

    \b
    Examples:
        export INSPIRE_TARGET_DIR="/train/logs"
        inspire job create --name "pr-123" --resource "4xH200" --command "cd /path/to/code && bash train.sh"
        inspire job create -n test -r H200 -c "python train.py" --priority 9
        inspire job create -n test -r 4xH200 -c "python train.py" --no-auto
    """
    try:
        config, _ = Config.from_files_and_env(require_target_dir=True)
        api = AuthManager.get_api(config)

        # Auto-select location based on GPU availability (if requested)
        if auto and not location:
            try:
                requested_gpu_type, requested_gpu_count = api.resource_manager.parse_resource_request(resource)
            except Exception as e:
                _handle_error(ctx, "ValidationError", f"Invalid resource spec: {e}", EXIT_VALIDATION_ERROR)
                return

            # Use accurate browser API for resource selection
            best = find_best_compute_group_accurate(
                gpu_type=requested_gpu_type.value,
                min_gpus=requested_gpu_count,
                include_preemptible=True,  # Count low-priority GPUs as available
                instance_count=nodes,
            )

            if not best:
                _handle_error(
                    ctx,
                    "InsufficientResources",
                    f"No {requested_gpu_type.value} compute group has at least {requested_gpu_count} available GPUs",
                    EXIT_VALIDATION_ERROR,
                )
                return

            # Map group_id -> location string expected by ResourceManager
            selected_group_name = best.group_name
            selected_location = None
            for group in api.resource_manager.compute_groups:
                if group.compute_group_id == best.group_id:
                    selected_group_name = group.name
                    selected_location = group.location
                    break

            if not selected_location:
                # Fall back to group name if we can't map it
                selected_location = selected_group_name

            location = selected_location

            if not ctx.json_output:
                if best.selection_source == "nodes" and best.free_nodes:
                    click.echo(
                        "Auto-selected: "
                        f"{selected_group_name}, {best.free_nodes} full nodes free "
                        f"({best.available_gpus} GPUs)"
                    )
                else:
                    preempt_note = (
                        f" (+{best.low_priority_gpus} preemptible)"
                        if best.low_priority_gpus > 0
                        else ""
                    )
                    click.echo(
                        f"Auto-selected: {selected_group_name}, {best.available_gpus} GPUs available{preempt_note}"
                    )

        # Wrap in bash for consistent shell behavior
        command = _wrap_in_bash(command)

        # Prepend remote_env exports to command
        env_exports = build_env_exports(config.remote_env)

        # If INSPIRE_TARGET_DIR is configured, wrap the command so
        # stdout/stderr land in a single master log file under
        # ${INSPIRE_TARGET_DIR}/.inspire/.
        final_command = f"{env_exports}{command}" if env_exports else command
        log_path = None
        if config.target_dir:
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            log_dir = os.path.join(config.target_dir, ".inspire")
            log_filename = f"training_master_{timestamp}.log"
            log_path = os.path.join(log_dir, log_filename)
            final_command = f'{env_exports}mkdir -p "{log_dir}" && ( {command} ) > "{log_path}" 2>&1'

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
            instance_count=nodes,
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
                if nodes > 1:
                    click.echo(f"Nodes:    {nodes}")
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


@job.command("command")
@click.argument("job_id")
@pass_context
def show_command(ctx: Context, job_id: str):
    """Show the training command used for a job."""
    # Validate job ID format early (before auth/API calls)
    format_error = _validate_job_id_format(job_id)
    if format_error:
        _handle_error(ctx, "InvalidJobID", format_error, EXIT_JOB_NOT_FOUND)
        return

    cached_command = None
    cache = JobCache(os.getenv("INSPIRE_JOB_CACHE"))
    cached_job = cache.get_job(job_id)
    if cached_job:
        cached_command = cached_job.get("command")

    command_value = None
    source = None

    try:
        config = Config.from_env()
        api = AuthManager.get_api(config)

        result = api.get_job_detail(job_id)
        job_data = result.get("data", {})
        command_value = job_data.get("command")
        if command_value:
            source = "api"
    except ConfigError as e:
        if not cached_command:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
    except AuthenticationError as e:
        if not cached_command:
            _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
            return
    except Exception as e:
        if not cached_command:
            if "not found" in str(e).lower() or "invalid job id" in str(e).lower():
                _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
            else:
                _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return

    if not command_value and cached_command:
        command_value = cached_command
        source = "cache"

    if not command_value:
        _handle_error(
            ctx,
            "CommandNotFound",
            f"No command found for job {job_id}",
            EXIT_API_ERROR,
        )
        return

    if ctx.json_output:
        payload = {"job_id": job_id, "command": command_value}
        if source:
            payload["source"] = source
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo(command_value)


@job.command("stop")
@click.argument("job_id")
@pass_context
def stop(ctx: Context, job_id: str):
    """Stop a running training job.

    \b
    Example:
        inspire job stop job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf
    """
    # Validate job ID format early (before auth/API calls)
    format_error = _validate_job_id_format(job_id)
    if format_error:
        _handle_error(ctx, "InvalidJobID", format_error, EXIT_JOB_NOT_FOUND)
        return

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
@click.option(
    "--active", "-a",
    is_flag=True,
    help="Show only active jobs (exclude failed, cancelled, stopped)"
)
@click.option("--watch", "-w", is_flag=True, help="Continuously refresh job list")
@click.option("--interval", type=int, default=10, help="Refresh interval in seconds for --watch (default: 10)")
@pass_context
def list_jobs(ctx: Context, limit: int, status: str, active: bool, watch: bool, interval: int):
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
            )
            return

        cache = JobCache(config.get_expanded_cache_path())

        # Define statuses to exclude when --active flag is set
        exclude_statuses = None
        if active:
            exclude_statuses = {
                "FAILED", "job_failed",
                "CANCELLED", "job_cancelled",
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
@click.option("--head", type=int, help="Show first N lines only")
@click.option("--path", is_flag=True, help="Just print log path, don't read content")
@click.option("--refresh", is_flag=True, help="Re-fetch log from the beginning (ignore cached offset)")
@click.option("--follow", "-f", is_flag=True, help="Continuously poll for new log content")
@click.option("--interval", type=int, default=30, help="Poll interval for --follow in seconds (default: 30)")
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
):
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
        _bulk_update_logs(ctx, status=status, limit=limit, refresh=refresh)
        return

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

        # Try SSH tunnel first for fast log access
        try:
            if is_tunnel_available():
                if follow:
                    # Real-time streaming via SSH
                    if not ctx.json_output:
                        click.echo("Using SSH tunnel (fast path)")
                    final_status = _follow_logs_via_ssh(
                        job_id=job_id,
                        config=config,
                        remote_log_path=str(remote_log_path_str),
                        tail_lines=tail or 50,
                    )
                    # Exit code based on job status
                    if final_status in {"SUCCEEDED", "job_succeeded"}:
                        sys.exit(EXIT_SUCCESS)
                    elif final_status in {"FAILED", "CANCELLED", "job_failed", "job_cancelled"}:
                        sys.exit(EXIT_GENERAL_ERROR)
                    else:
                        # User interrupted or status unknown
                        sys.exit(EXIT_SUCCESS)
                else:
                    # One-time fetch via SSH
                    if not ctx.json_output:
                        click.echo("Using SSH tunnel (fast path)")

                    content = _fetch_log_via_ssh(
                        remote_log_path=str(remote_log_path_str),
                        tail=tail,
                        head=head,
                    )

                    if path:
                        # Just show path
                        if ctx.json_output:
                            click.echo(json_formatter.format_json({
                                "job_id": job_id,
                                "log_path": str(remote_log_path_str),
                            }))
                        else:
                            click.echo(str(remote_log_path_str))
                    else:
                        # Show content
                        if ctx.json_output:
                            click.echo(json_formatter.format_json({
                                "job_id": job_id,
                                "log_path": str(remote_log_path_str),
                                "content": content,
                                "method": "ssh_tunnel",
                            }))
                        else:
                            if tail:
                                click.echo(f"=== Last {tail} lines ===\n")
                            elif head:
                                click.echo(f"=== First {head} lines ===\n")
                            click.echo(content)

                    sys.exit(EXIT_SUCCESS)

        except TunnelNotAvailableError:
            if not ctx.json_output:
                click.echo("Tunnel not available, using Gitea workflow...", err=True)
        except IOError as e:
            if not ctx.json_output:
                click.echo(f"SSH log fetch failed: {e}", err=True)
                click.echo("Falling back to Gitea workflow...", err=True)

        # Handle --path mode (just show path, no fetch)
        if path:
            if ctx.json_output:
                click.echo(json_formatter.format_json({
                    "job_id": job_id,
                    "log_path": str(remote_log_path_str),
                }))
            else:
                click.echo(str(remote_log_path_str))
            sys.exit(EXIT_SUCCESS)

        # Handle --follow mode (Gitea fallback)
        if follow:
            _follow_logs(
                ctx=ctx,
                config=config,
                cache=cache,
                job_id=job_id,
                remote_log_path=str(remote_log_path_str),
                cache_path=cache_path,
                refresh=refresh,
                interval=interval,
            )
            return

        # Get current offset from cache (0 if refresh or first time)
        current_offset = 0 if refresh else cache.get_log_offset(job_id)

        # Reset offset if cache file missing but offset > 0
        if current_offset > 0 and not cache_path.exists():
            current_offset = 0
            cache.reset_log_offset(job_id)

        # Determine fetch strategy
        if current_offset > 0 and cache_path.exists():
            # Incremental fetch
            if not ctx.json_output:
                click.echo(f"Fetching new log content from offset {current_offset}...")

            try:
                _, bytes_added = fetch_remote_log_incremental(
                    config=config,
                    job_id=job_id,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    start_offset=current_offset,
                )
                # Update offset
                cache.set_log_offset(job_id, current_offset + bytes_added)
                if not ctx.json_output and bytes_added == 0:
                    click.echo(
                        "No new content. If log was rotated, use --refresh.",
                        err=True
                    )
            except GiteaAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                _handle_error(ctx, "Timeout", str(e), EXIT_TIMEOUT)
            except GiteaError as e:
                error_msg = (
                    f"{str(e)}\n\n"
                    f"Hints:\n"
                    f"- Check that the training job created a log file at: {remote_log_path_str}\n"
                    f"- Verify the Bridge workflow exists and can access the shared filesystem\n"
                    f"- View Gitea Actions at: {config.gitea_server}/{config.gitea_repo}/actions"
                )
                _handle_error(ctx, "RemoteLogError", error_msg, EXIT_GENERAL_ERROR)
        elif refresh or not cache_path.exists():
            # Full fetch (first time or refresh)
            if not ctx.json_output:
                click.echo(
                    "Fetching remote log via Gitea workflow (first fetch may take ~10-30s)..."
                )

            try:
                fetch_remote_log_via_bridge(
                    config=config,
                    job_id=job_id,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    refresh=refresh,
                )
                # Update offset to file size
                if cache_path.exists():
                    new_offset = cache_path.stat().st_size
                    cache.set_log_offset(job_id, new_offset)
            except GiteaAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                _handle_error(ctx, "Timeout", str(e), EXIT_TIMEOUT)
            except GiteaError as e:
                error_msg = (
                    f"{str(e)}\n\n"
                    f"Hints:\n"
                    f"- Check that the training job created a log file at: {remote_log_path_str}\n"
                    f"- Verify the Bridge workflow exists and can access the shared filesystem\n"
                    f"- View Gitea Actions at: {config.gitea_server}/{config.gitea_repo}/actions"
                )
                _handle_error(ctx, "RemoteLogError", error_msg, EXIT_GENERAL_ERROR)

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

        # Print head
        if head:
            try:
                with cache_path.open("r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
                head_lines = lines[:head] if head > 0 else lines
                if ctx.json_output:
                    click.echo(
                        json_formatter.format_json(
                            {
                                "log_path": str(cache_path),
                                "lines": head_lines,
                                "count": len(head_lines),
                            }
                        )
                    )
                else:
                    click.echo(f"=== First {len(head_lines)} lines ===\n")
                    for line in head_lines:
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


def _follow_logs(
    ctx: Context,
    config: Config,
    cache: JobCache,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    refresh: bool,
    interval: int,
) -> None:
    """Continuously fetch and display new log content."""
    # Initialize API client for status checking
    api = AuthManager.get_api(config)
    terminal_statuses = {
        "SUCCEEDED", "FAILED", "CANCELLED",
        "job_succeeded", "job_failed", "job_cancelled",
    }
    final_status = None

    try:
        # Get current offset
        current_offset = 0 if refresh else cache.get_log_offset(job_id)

        # Initial fetch if needed
        if refresh or not cache_path.exists():
            if not ctx.json_output:
                click.echo(f"Fetching log for job {job_id}...")

            try:
                fetch_remote_log_via_bridge(
                    config=config,
                    job_id=job_id,
                    remote_log_path=remote_log_path,
                    cache_path=cache_path,
                    refresh=refresh,
                )
                current_offset = cache_path.stat().st_size
                cache.set_log_offset(job_id, current_offset)
            except (GiteaAuthError, GiteaError, TimeoutError) as e:
                _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)

        # Display existing content
        if cache_path.exists():
            content = cache_path.read_text(encoding="utf-8", errors="replace")
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "event": "initial_content",
                            "job_id": job_id,
                            "size_bytes": len(content),
                            "content": content,
                        }
                    )
                )
            else:
                click.echo(content, nl=False)

            # Sync offset with actual file size (fixes stale/missing cache offset)
            current_offset = cache_path.stat().st_size
            cache.set_log_offset(job_id, current_offset)

        # Track last displayed position
        last_displayed = current_offset

        if not ctx.json_output:
            click.echo(f"\n--- Following log (interval: {interval}s, Ctrl+C to stop) ---")

        while True:
            time.sleep(interval)

            try:
                # Remember size before fetch
                size_before = cache_path.stat().st_size if cache_path.exists() else 0

                # Fetch full log (more robust than incremental)
                fetch_remote_log_via_bridge(
                    config=config,
                    job_id=job_id,
                    remote_log_path=remote_log_path,
                    cache_path=cache_path,
                    refresh=True,  # Always get latest
                )

                # Calculate actual new bytes
                size_after = cache_path.stat().st_size if cache_path.exists() else 0
                bytes_added = size_after - last_displayed

                if bytes_added > 0:
                    # Update offset
                    current_offset = size_after
                    cache.set_log_offset(job_id, current_offset)

                    # Display only the new content
                    with cache_path.open("rb") as f:
                        f.seek(last_displayed)
                        new_content = f.read().decode("utf-8", errors="replace")

                    if ctx.json_output:
                        click.echo(
                            json_formatter.format_json(
                                {
                                    "event": "new_content",
                                    "job_id": job_id,
                                    "bytes_added": bytes_added,
                                    "offset": current_offset,
                                    "content": new_content,
                                }
                            )
                        )
                    else:
                        click.echo(new_content, nl=False)

                    last_displayed = current_offset

            except (GiteaError, TimeoutError) as e:
                if not ctx.json_output:
                    click.echo(f"\nWarning: Fetch failed: {e}", err=True)

            # Check job status
            try:
                result = api.get_job_detail(job_id)
                job_data = result.get("data", {})
                current_status = job_data.get("status", "UNKNOWN")
                cache.update_status(job_id, current_status)

                if current_status in terminal_statuses:
                    final_status = current_status
                    break  # Exit loop to do final fetch
            except Exception as e:
                if not ctx.json_output:
                    click.echo(f"\nWarning: Status check failed: {e}", err=True)

        # Grace period for final logs after job completion
        if final_status:
            time.sleep(5)
            # One final log fetch
            try:
                fetch_remote_log_via_bridge(
                    config=config,
                    job_id=job_id,
                    remote_log_path=remote_log_path,
                    cache_path=cache_path,
                    refresh=True,
                )
                # Display any remaining content
                size_after = cache_path.stat().st_size if cache_path.exists() else 0
                bytes_added = size_after - last_displayed
                if bytes_added > 0:
                    with cache_path.open("rb") as f:
                        f.seek(last_displayed)
                        new_content = f.read().decode("utf-8", errors="replace")
                    if ctx.json_output:
                        click.echo(
                            json_formatter.format_json(
                                {
                                    "event": "final_content",
                                    "job_id": job_id,
                                    "bytes_added": bytes_added,
                                    "content": new_content,
                                }
                            )
                        )
                    else:
                        click.echo(new_content, nl=False)
            except (GiteaError, TimeoutError):
                pass

            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "event": "job_completed",
                            "job_id": job_id,
                            "status": final_status,
                        }
                    )
                )
            else:
                click.echo(f"\nJob completed with status: {final_status}")

            if final_status in {"SUCCEEDED", "job_succeeded"}:
                sys.exit(EXIT_SUCCESS)
            else:
                sys.exit(EXIT_GENERAL_ERROR)

    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nStopped following.")
        sys.exit(EXIT_SUCCESS)
    except GiteaAuthError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


def _watch_jobs(
    ctx: Context,
    config: Config,
    limit: int,
    status: Optional[str],
    active: bool,
    interval: int,
) -> None:
    """Continuously poll and display job status with incremental updates."""
    # Suppress API logging during watch mode to keep display clean
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    cache = JobCache(config.get_expanded_cache_path())

    # Show auth message
    if not ctx.json_output:
        click.echo("🔐 Authenticating...")

    try:
        api = AuthManager.get_api(config)
    except AuthenticationError as e:
        api_logger.setLevel(original_level)
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)

    # Build exclude set for --active
    exclude_statuses = None
    if active:
        exclude_statuses = {"FAILED", "job_failed", "CANCELLED", "job_cancelled", "job_stopped"}

    # Terminal statuses - jobs that have finished
    terminal_statuses = {
        "SUCCEEDED", "job_succeeded",
        "FAILED", "job_failed",
        "CANCELLED", "job_cancelled",
        "job_stopped",
    }

    # Track jobs that completed during this watch session
    completed_this_session: list = []
    completed_job_ids: set = set()

    def _progress_bar(current: int, total: int, width: int = 20) -> str:
        """Generate a cute progress bar."""
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
        """Clear screen and render job table with progress bar."""
        os.system('clear')
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

            # Show completed jobs section if any
            if completed_list:
                click.echo(f"\n✅ Completed This Session ({len(completed_list)})")
                click.echo("─" * 60)
                for job in completed_list:
                    status_emoji = "✅" if "succeeded" in job.get("status", "").lower() else "❌"
                    click.echo(
                        f"{job.get('job_id', 'N/A')[:36]:36}  "
                        f"{job.get('name', 'N/A')[:20]:20}  "
                        f"{status_emoji} {job.get('status', 'N/A')}"
                    )

    try:
        while True:
            # Get jobs from cache
            jobs = cache.list_jobs(limit=limit, status=status, exclude_statuses=exclude_statuses)
            total = len(jobs)

            # Initial display with cached statuses
            _render_display(jobs, 0, total, completed_this_session)

            # Update each job's status from API with incremental display refresh
            for i, job_item in enumerate(jobs):
                job_id = job_item.get("job_id")
                if job_id:
                    original_status = job_item.get("status", "")
                    try:
                        result = api.get_job_detail(job_id)
                        data = result.get("data", {})
                        new_status = data.get("status")
                        if new_status:
                            job_item["status"] = new_status  # Update in-memory
                            cache.update_status(job_id, new_status)  # Persist

                            # Check if job just completed (transitioned to terminal status)
                            if (
                                new_status in terminal_statuses
                                and original_status not in terminal_statuses
                                and job_id not in completed_job_ids
                            ):
                                completed_this_session.append(dict(job_item))
                                completed_job_ids.add(job_id)
                    except Exception:
                        pass  # Keep cached status on error

                # Single redraw after each job poll (progress bar updates)
                _render_display(jobs, i + 1, total, completed_this_session)

                # Delay between API calls to avoid rate limiting (skip after last)
                if i < total - 1:
                    time.sleep(1.0)

            # Re-filter after status updates (jobs may have changed status)
            if active and exclude_statuses:
                filtered = [j for j in jobs if j.get("status") not in exclude_statuses]
                if len(filtered) != len(jobs):
                    _render_display(filtered, total, total, completed_this_session)

            # Wait for next refresh cycle
            time.sleep(interval)

    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nStopped watching.")
        sys.exit(EXIT_SUCCESS)
    finally:
        # Restore original logging level
        api_logger.setLevel(original_level)


def _bulk_update_logs(
    ctx: Context,
    status: tuple,
    limit: int,
    refresh: bool,
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


def _fetch_log_via_ssh(
    remote_log_path: str,
    tail: Optional[int] = None,
    head: Optional[int] = None,
) -> str:
    """Fetch log content via SSH tunnel.

    Args:
        remote_log_path: Path to log file on Bridge
        tail: If set, return last N lines
        head: If set, return first N lines

    Returns:
        Log content as string

    Raises:
        TunnelNotAvailableError: If tunnel is not available
        IOError: If log file cannot be read
    """
    if tail:
        command = f"tail -n {tail} '{remote_log_path}'"
    elif head:
        command = f"head -n {head} '{remote_log_path}'"
    else:
        command = f"cat '{remote_log_path}'"

    result = run_ssh_command(command=command, capture_output=True)

    if result.returncode != 0:
        raise IOError(f"Failed to read log file: {result.stderr}")

    return result.stdout


def _follow_logs_via_ssh(
    job_id: str,
    config: Config,
    remote_log_path: str,
    tail_lines: int = 50,
    wait_timeout: int = 300,
) -> Optional[str]:
    """Stream log content via SSH tail -f with auto-stop on job completion.

    This uses SSH's tail -f for real-time streaming.
    Waits for the log file to exist if the job is still queuing.
    Periodically checks job status and stops when job reaches terminal state.

    Args:
        job_id: The job ID to monitor
        config: CLI configuration for API access
        remote_log_path: Path to log file on Bridge
        tail_lines: Initial number of lines to show
        wait_timeout: Max seconds to wait for log file to appear (default: 300)

    Returns:
        Final job status if job completed, None if interrupted by user
    """
    import select
    import subprocess
    import time
    from inspire.cli.utils.tunnel import get_ssh_command_args, run_ssh_command

    # Suppress API logging during streaming to keep output clean
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    # Initialize API client for status checking
    api = AuthManager.get_api(config)
    terminal_statuses = {
        "SUCCEEDED", "FAILED", "CANCELLED",
        "job_succeeded", "job_failed", "job_cancelled",
    }
    final_status = None
    status_check_interval = 5  # Check status every 5 seconds

    click.echo(f"Log file: {remote_log_path}")

    # Wait for log file to exist (job may be queuing)
    check_cmd = f"test -f '{remote_log_path}' && echo 'exists' || echo 'waiting'"
    start_time = time.time()
    file_exists = False

    while time.time() - start_time < wait_timeout:
        try:
            result = run_ssh_command(check_cmd, timeout=10)
            if "exists" in result.stdout:
                file_exists = True
                break
        except Exception:
            pass

        elapsed = int(time.time() - start_time)
        click.echo(f"\rWaiting for job to start... ({elapsed}s)", nl=False)
        time.sleep(5)

    if not file_exists:
        click.echo(f"\n\nTimeout: Log file not created after {wait_timeout}s")
        click.echo("Job may still be queuing. Check status with: inspire job status <job_id>")
        return

    click.echo(f"\nJob started! Following logs...")
    click.echo(f"(showing last {tail_lines} lines, then following new content)")
    click.echo("Press Ctrl+C to stop\n")

    # Build command: show last N lines then follow
    command = f"tail -n {tail_lines} -f '{remote_log_path}'"
    ssh_args = get_ssh_command_args(remote_command=command)

    process = None
    try:
        # Run SSH with real-time output
        process = subprocess.Popen(
            ssh_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )

        # Use select for non-blocking I/O with periodic status checks
        last_status_check = time.time()

        while True:
            # Check if process has ended
            if process.poll() is not None:
                # Drain any remaining output
                for line in process.stdout:
                    click.echo(line, nl=False)
                break

            # Use select to wait for output with timeout
            ready, _, _ = select.select([process.stdout], [], [], 1.0)

            if ready:
                line = process.stdout.readline()
                if line:
                    click.echo(line, nl=False)
                elif process.poll() is not None:
                    # EOF reached (process exited)
                    break
                # else: temporary no data, continue waiting

            # Periodically check job status
            current_time = time.time()
            if current_time - last_status_check >= status_check_interval:
                last_status_check = current_time
                try:
                    result = api.get_job_detail(job_id)
                    job_data = result.get("data", {})
                    current_status = job_data.get("status", "UNKNOWN")

                    if current_status in terminal_statuses:
                        final_status = current_status
                        # Grace period: wait a bit for final logs
                        time.sleep(3)
                        # Drain remaining output
                        process.stdout.close()
                        break
                except Exception:
                    # Status check failed, continue streaming
                    pass

        # Show completion message
        if final_status:
            click.echo(f"\n\nJob completed with status: {final_status}")

    except KeyboardInterrupt:
        click.echo("\n\nStopped following logs.")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        # Restore API logging level
        api_logger.setLevel(original_level)

    return final_status


def _handle_error(ctx: Context, error_type: str, message: str, exit_code: int):
    """Handle and format errors consistently."""
    if ctx.json_output:
        click.echo(json_formatter.format_json_error(error_type, message, exit_code), err=True)
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(exit_code)
