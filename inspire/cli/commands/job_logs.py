"""Job logs command."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import click

from inspire.cli.commands.job_common import _ensure_valid_job_id
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_LOG_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.auth import AuthManager
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.gitea import (
    GiteaAuthError,
    GiteaError,
    fetch_remote_log_incremental,
)
from inspire.cli.utils.tunnel import (
    TunnelNotAvailableError,
    is_tunnel_available,
    run_ssh_command,
)


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

        if not _ensure_valid_job_id(ctx, job_id):
            return

        try:
            config = Config.from_env(require_target_dir=False)
            cache = deps.JobCache(config.get_expanded_cache_path())

            # Resolve job from cache
            cached = cache.get_job(job_id)
            if not cached:
                _handle_error(ctx, "JobNotFound", f"Job not found: {job_id}", EXIT_JOB_NOT_FOUND)
                return

            remote_log_path_str = cached.get("log_path")
            if not remote_log_path_str:
                _handle_error(
                    ctx,
                    "LogNotFound",
                    f"No log file found for job {job_id}",
                    EXIT_LOG_NOT_FOUND,
                )
                return

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
                        elif final_status in {
                            "FAILED",
                            "CANCELLED",
                            "job_failed",
                            "job_cancelled",
                        }:
                            sys.exit(EXIT_GENERAL_ERROR)
                        else:
                            # User interrupted or status unknown
                            sys.exit(EXIT_SUCCESS)

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
                            click.echo(
                                json_formatter.format_json(
                                    {
                                        "job_id": job_id,
                                        "log_path": str(remote_log_path_str),
                                    }
                                )
                            )
                        else:
                            click.echo(str(remote_log_path_str))
                    else:
                        # Show content
                        if ctx.json_output:
                            click.echo(
                                json_formatter.format_json(
                                    {
                                        "job_id": job_id,
                                        "log_path": str(remote_log_path_str),
                                        "content": content,
                                        "method": "ssh_tunnel",
                                    }
                                )
                            )
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
                    click.echo(
                        json_formatter.format_json(
                            {
                                "job_id": job_id,
                                "log_path": str(remote_log_path_str),
                            }
                        )
                    )
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
                    deps=deps,
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
                        click.echo("No new content. If log was rotated, use --refresh.", err=True)
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
                    deps.fetch_remote_log_via_bridge(
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

    return logs


def _follow_logs(
    ctx: Context,
    config: Config,
    cache,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    refresh: bool,
    interval: int,
    *,
    deps,
) -> None:
    """Continuously fetch and display new log content."""
    # Initialize API client for status checking
    api = AuthManager.get_api(config)
    terminal_statuses = {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "job_succeeded",
        "job_failed",
        "job_cancelled",
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
                deps.fetch_remote_log_via_bridge(
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
            deps.time.sleep(interval)

            try:
                # Fetch full log (more robust than incremental)
                deps.fetch_remote_log_via_bridge(
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
            deps.time.sleep(5)
            # One final log fetch
            try:
                deps.fetch_remote_log_via_bridge(
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


def _fetch_log_via_ssh(
    remote_log_path: str,
    tail: Optional[int] = None,
    head: Optional[int] = None,
) -> str:
    """Fetch log content via SSH tunnel."""
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
    """Stream log content via SSH tail -f with auto-stop on job completion."""
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
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "job_succeeded",
        "job_failed",
        "job_cancelled",
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
        return None

    click.echo("\nJob started! Following logs...")
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
