"""Job stop command."""

from __future__ import annotations

import click

from inspire.cli.commands.job_common import _ensure_valid_job_id
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_JOB_NOT_FOUND,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error


def build_stop_command(deps) -> click.Command:
    @click.command("stop")
    @click.argument("job_id")
    @pass_context
    def stop(ctx: Context, job_id: str) -> None:
        """Stop a running training job.

        \b
        Example:
            inspire job stop job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf
        """
        if not _ensure_valid_job_id(ctx, job_id):
            return

        try:
            config = Config.from_env()
            api = AuthManager.get_api(config)

            api.stop_training_job(job_id)

            # Update local cache
            cache = deps.JobCache(config.get_expanded_cache_path())
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
            msg = str(e).lower()
            if "not found" in msg or "invalid job id" in msg:
                _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
            else:
                _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)

    return stop
