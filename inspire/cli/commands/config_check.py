"""`inspire config check` implementation."""

from __future__ import annotations

import sys

import click

from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error


@click.command("check")
@pass_context
def check_config(ctx: Context) -> None:
    """Check environment configuration and API authentication.

    Verifies configuration (from files and environment) and attempts to
    authenticate with the Inspire API.
    """
    try:
        cfg, _sources = Config.from_files_and_env(require_credentials=True)
        auth_ok = True
        auth_error = None

        # Attempt authentication
        try:
            AuthManager.get_api(cfg)
        except AuthenticationError as e:
            auth_ok = False
            auth_error = str(e)

        result = {
            "username": cfg.username,
            "base_url": cfg.base_url,
            "target_dir": cfg.target_dir,
            "job_cache_path": cfg.get_expanded_cache_path(),
            "log_pattern": cfg.log_pattern,
            "timeout": cfg.timeout,
            "max_retries": cfg.max_retries,
            "retry_delay": cfg.retry_delay,
            "auth_ok": auth_ok,
        }
        if auth_error:
            result["auth_error"] = auth_error

        if ctx.json_output:
            click.echo(json_formatter.format_json(result, success=auth_ok))
        else:
            if auth_ok:
                click.echo(human_formatter.format_success("Configuration looks good"))
            else:
                click.echo(human_formatter.format_error("Authentication failed"))

            click.echo(f"\nUsername:     {cfg.username}")
            click.echo(f"Base URL:     {cfg.base_url}")
            click.echo(f"Target dir:   {cfg.target_dir or '(not set - required for logs)'}")
            click.echo(f"Log pattern:  {cfg.log_pattern}")
            click.echo(f"Job cache:    {cfg.get_expanded_cache_path()}")
            click.echo(f"Timeout:      {cfg.timeout}s")
            click.echo(f"Max retries:  {cfg.max_retries}")
            click.echo(f"Retry delay:  {cfg.retry_delay}s")

            if auth_error:
                click.echo(f"\nDetails: {auth_error}")

        # Exit non-zero if auth failed when not in JSON mode
        if not auth_ok:
            sys.exit(EXIT_AUTH_ERROR)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
