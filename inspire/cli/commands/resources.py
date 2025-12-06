"""Resource commands for Inspire CLI."""

import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_API_ERROR,
)
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.formatters import json_formatter, human_formatter


@click.group()
def resources():
    """View available compute resources."""
    pass


@resources.command("list")
@pass_context
def list_resources(ctx: Context):
    """List all available GPU configurations.

    Shows available GPU specs and compute groups that can be used
    with the --resource option when creating jobs.

    \b
    Example:
        inspire resources list
    """
    try:
        config = Config.from_env()
        api = AuthManager.get_api(config)

        # Get resource specs and groups from the resource manager
        rm = api.resource_manager

        specs = [
            {
                "gpu_type": spec.gpu_type.value,
                "gpu_count": spec.gpu_count,
                "cpu_cores": spec.cpu_cores,
                "memory_gb": spec.memory_gb,
                "gpu_memory_gb": spec.gpu_memory_gb,
                "spec_id": spec.spec_id,
                "description": spec.description,
            }
            for spec in rm.resource_specs
        ]

        groups = [
            {
                "name": group.name,
                "compute_group_id": group.compute_group_id,
                "gpu_type": group.gpu_type.value,
                "location": group.location,
            }
            for group in rm.compute_groups
        ]

        if ctx.json_output:
            click.echo(json_formatter.format_json({
                "specs": specs,
                "compute_groups": groups,
            }))
        else:
            click.echo(human_formatter.format_resources(specs, groups))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _handle_error(ctx: Context, error_type: str, message: str, exit_code: int):
    """Handle and format errors consistently."""
    if ctx.json_output:
        click.echo(json_formatter.format_json_error(error_type, message, exit_code), err=True)
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(exit_code)
