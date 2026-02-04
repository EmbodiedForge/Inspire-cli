"""Single-job mode handler for `inspire job logs` (façade)."""

from __future__ import annotations

import sys

import click

from inspire.cli.commands._impl.job_logs.single_cache import (
    build_log_cache_paths,
    get_current_log_offset,
    migrate_legacy_log_filename,
    update_log_offset_to_filesize,
)
from inspire.cli.commands._impl.job_logs.single_fetch import (
    fetch_log_full_via_bridge,
    fetch_log_incremental,
    format_remote_log_error_message,
)
from inspire.cli.commands._impl.job_logs.single_output import (
    echo_file_content,
    echo_file_head,
    echo_file_tail,
    echo_log_path,
)
from inspire.cli.commands._impl.job_logs.single_ssh import try_get_ssh_exit_code
from inspire.cli.commands.job_logs_helpers import _follow_logs
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_LOG_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
)
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.gitea import GiteaAuthError, GiteaError


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

        cache_paths = build_log_cache_paths(config, job_id)
        cache_path = migrate_legacy_log_filename(cache_paths)

        ssh_exit_code = try_get_ssh_exit_code(
            ctx,
            config=config,
            job_id=job_id,
            remote_log_path=str(remote_log_path_str),
            tail=tail,
            head=head,
            path=path,
            follow=follow,
        )
        if ssh_exit_code is not None:
            sys.exit(ssh_exit_code)

        if path:
            echo_log_path(ctx, job_id=job_id, remote_log_path=str(remote_log_path_str))
            sys.exit(EXIT_SUCCESS)

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

        current_offset = get_current_log_offset(
            cache,
            job_id=job_id,
            cache_path=cache_path,
            refresh=refresh,
        )

        if current_offset > 0 and cache_path.exists():
            if not ctx.json_output:
                click.echo(f"Fetching new log content from offset {current_offset}...")

            try:
                bytes_added = fetch_log_incremental(
                    config=config,
                    job_id=job_id,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    start_offset=current_offset,
                )
                cache.set_log_offset(job_id, current_offset + bytes_added)
                if not ctx.json_output and bytes_added == 0:
                    click.echo("No new content. If log was rotated, use --refresh.", err=True)
            except GiteaAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                _handle_error(ctx, "Timeout", str(e), EXIT_TIMEOUT)
            except GiteaError as e:
                error_msg = format_remote_log_error_message(
                    e,
                    remote_log_path=str(remote_log_path_str),
                    config=config,
                )
                _handle_error(ctx, "RemoteLogError", error_msg, EXIT_GENERAL_ERROR)
        elif refresh or not cache_path.exists():
            if not ctx.json_output:
                click.echo(
                    "Fetching remote log via Gitea workflow (first fetch may take ~10-30s)..."
                )

            try:
                fetch_log_full_via_bridge(
                    deps=deps,
                    config=config,
                    job_id=job_id,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    refresh=refresh,
                )
                update_log_offset_to_filesize(cache, job_id=job_id, cache_path=cache_path)
            except GiteaAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                _handle_error(ctx, "Timeout", str(e), EXIT_TIMEOUT)
            except GiteaError as e:
                error_msg = format_remote_log_error_message(
                    e,
                    remote_log_path=str(remote_log_path_str),
                    config=config,
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

        if tail:
            try:
                echo_file_tail(ctx, cache_path=cache_path, tail=tail)
            except OSError as e:
                _handle_error(ctx, "LogNotFound", str(e), EXIT_LOG_NOT_FOUND)
            return

        if head:
            try:
                echo_file_head(ctx, cache_path=cache_path, head=head)
            except OSError as e:
                _handle_error(ctx, "LogNotFound", str(e), EXIT_LOG_NOT_FOUND)
            return

        try:
            echo_file_content(ctx, cache_path=cache_path)
        except OSError as e:
            _handle_error(ctx, "LogNotFound", str(e), EXIT_LOG_NOT_FOUND)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


__all__ = ["run_job_logs_single_job"]
