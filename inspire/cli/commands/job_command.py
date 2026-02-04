"""Job command subcommand (show training command)."""

from __future__ import annotations

import os

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_JOB_NOT_FOUND,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.job_cli import ensure_valid_job_id
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error


def build_command_command(deps) -> click.Command:
    @click.command("command")
    @click.argument("job_id")
    @pass_context
    def show_command(ctx: Context, job_id: str) -> None:
        """Show the training command used for a job."""
        if not ensure_valid_job_id(ctx, job_id):
            return

        cached_command = None
        cache = deps.JobCache(os.getenv("INSPIRE_JOB_CACHE"))
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
                msg = str(e).lower()
                if "not found" in msg or "invalid job id" in msg:
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

    return show_command
