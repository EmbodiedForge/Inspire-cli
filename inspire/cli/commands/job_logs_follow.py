"""Follow mode for `inspire job logs` (Gitea/workflow path)."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, EXIT_SUCCESS
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.auth import AuthManager
from inspire.cli.utils.config import Config
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.gitea import GiteaAuthError, GiteaError


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

        # Final fetch to get complete log
        if final_status:
            if not ctx.json_output:
                click.echo(f"\n--- Job completed with status: {final_status} ---")
                click.echo("Fetching final log content...")

            deps.fetch_remote_log_via_bridge(
                config=config,
                job_id=job_id,
                remote_log_path=remote_log_path,
                cache_path=cache_path,
                refresh=True,
            )

            # Display final portion if new content
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
                                "status": final_status,
                                "bytes_added": bytes_added,
                                "content": new_content,
                            }
                        )
                    )
                else:
                    click.echo(new_content, nl=False)

        # Exit with appropriate code based on status
        if final_status in {"SUCCEEDED", "job_succeeded"}:
            sys.exit(EXIT_SUCCESS)
        if final_status in {"FAILED", "CANCELLED", "job_failed", "job_cancelled"}:
            sys.exit(EXIT_GENERAL_ERROR)
        sys.exit(EXIT_SUCCESS)

    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nStopped following.")
        sys.exit(EXIT_SUCCESS)
    except GiteaAuthError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
