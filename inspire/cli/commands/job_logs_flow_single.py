"""Single-job mode handler for `inspire job logs`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from inspire.cli.commands.job_logs_helpers import (
    _fetch_log_via_ssh,
    _follow_logs,
    _follow_logs_via_ssh,
)
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_LOG_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.gitea import (
    GiteaAuthError,
    GiteaError,
    fetch_remote_log_incremental,
)
from inspire.cli.utils.tunnel import TunnelNotAvailableError, is_tunnel_available


def run_job_logs_single_job(
    ctx: Context,
    *,
    job_id: str,
    tail: int,
    head: int,
    path: bool,
    refresh: bool,
    follow: bool,
    interval: int,
    deps,
) -> None:
    """Run `inspire job logs JOB_ID` (single-job mode)."""
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


__all__ = ["run_job_logs_single_job"]
