"""Job wait command."""

from __future__ import annotations

import sys

import click

from inspire.cli.commands.job_common import _ensure_valid_job_id
from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error


def build_wait_command(deps) -> click.Command:
    @click.command("wait")
    @click.argument("job_id")
    @click.option(
        "--timeout", type=int, default=14400, help="Timeout in seconds (default: 4 hours)"
    )
    @click.option("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    @pass_context
    def wait(ctx: Context, job_id: str, timeout: int, interval: int) -> None:
        """Wait for a job to complete.

        Polls the job status until it reaches a terminal state
        (SUCCEEDED, FAILED, or CANCELLED).

        \b
        Example:
            inspire job wait job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --timeout 7200
        """
        if not _ensure_valid_job_id(ctx, job_id):
            return

        try:
            config = Config.from_env()
            api = AuthManager.get_api(config)
            cache = deps.JobCache(config.get_expanded_cache_path())

            terminal_statuses = {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",  # Uppercase
                "job_succeeded",
                "job_failed",
                "job_cancelled",  # API snake_case
            }
            start_time = deps.time.time()
            last_status = None

            click.echo(f"Waiting for job {job_id} (timeout: {timeout}s, interval: {interval}s)")

            while True:
                elapsed = deps.time.time() - start_time

                if elapsed > timeout:
                    _handle_error(ctx, "Timeout", f"Timeout after {timeout}s", EXIT_TIMEOUT)
                    return

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

                deps.time.sleep(interval)

        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        except AuthenticationError as e:
            _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        except KeyboardInterrupt:
            click.echo("\nInterrupted")
            sys.exit(EXIT_GENERAL_ERROR)

    return wait
