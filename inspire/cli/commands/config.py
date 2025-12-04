"""Configuration commands for Inspire CLI.

Commands:
    inspire config check - Validate environment and authentication
"""

import sys
import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_GENERAL_ERROR,
)
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.formatters import json_formatter, human_formatter


@click.group()
def config() -> None:
    """Inspect and validate Inspire CLI configuration."""
    pass


@config.command("check")
@pass_context
def check_config(ctx: Context) -> None:
    """Check environment configuration and API authentication.

    Verifies required environment variables and attempts to authenticate
    with the Inspire API.
    """
    try:
        config = Config.from_env()
        auth_ok = True
        auth_error = None

        # Attempt authentication
        try:
            AuthManager.get_api(config)
        except AuthenticationError as e:
            auth_ok = False
            auth_error = str(e)

        result = {
            "username": config.username,
            "base_url": config.base_url,
            "target_dir": config.target_dir,
            "job_cache_path": config.get_expanded_cache_path(),
            "log_pattern": config.log_pattern,
            "timeout": config.timeout,
            "max_retries": config.max_retries,
            "retry_delay": config.retry_delay,
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

            click.echo(f"\nUsername:     {config.username}")
            click.echo(f"Base URL:     {config.base_url}")
            click.echo(
                f"Target dir:   {config.target_dir or '(not set - required for logs)'}"
            )
            click.echo(f"Log pattern:  {config.log_pattern}")
            click.echo(f"Job cache:    {config.get_expanded_cache_path()}")
            click.echo(f"Timeout:      {config.timeout}s")
            click.echo(f"Max retries:  {config.max_retries}")
            click.echo(f"Retry delay:  {config.retry_delay}s")

            if auth_error:
                click.echo(f"\nDetails: {auth_error}")

        # Exit non-zero if auth failed when not in JSON mode
        if not auth_ok:
            sys.exit(EXIT_AUTH_ERROR)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


def _handle_error(ctx: Context, error_type: str, message: str, exit_code: int) -> None:
    """Handle and format errors consistently."""
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(error_type, message, exit_code),
            err=True,
        )
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(exit_code)
