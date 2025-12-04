"""Resource commands for Inspire CLI.

Commands:
    inspire resources list  - List available GPU configurations
    inspire resources check - Check availability of specific GPU type
"""

import sys
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


@resources.command("check")
@click.argument("gpu_type")
@pass_context
def check_resource(ctx: Context, gpu_type: str):
    """Check availability of a specific GPU type.

    Queries the API for available specs matching the GPU type.

    \b
    Examples:
        inspire resources check H200
        inspire resources check H100
    """
    try:
        config = Config.from_env()
        api = AuthManager.get_api(config)

        # Parse resource and get compute group
        rm = api.resource_manager
        try:
            _, compute_group_id = rm.get_recommended_config(gpu_type)
        except ValueError as e:
            if ctx.json_output:
                click.echo(json_formatter.format_json_error(
                    "ValidationError", str(e), EXIT_API_ERROR
                ))
            else:
                click.echo(human_formatter.format_error(str(e)))
            sys.exit(EXIT_API_ERROR)

        # Query available specs from API
        result = api.list_available_specs(compute_group_id)
        specs_data = result.get("data", {}).get("specs", [])

        if ctx.json_output:
            click.echo(json_formatter.format_json({
                "gpu_type": gpu_type.upper(),
                "compute_group_id": compute_group_id,
                "available_specs": specs_data,
            }))
        else:
            click.echo(f"\n\U0001f50d Checking availability for {gpu_type.upper()}...\n")

            if specs_data:
                click.echo(f"\u2705 {len(specs_data)} configuration(s) available:\n")
                for spec in specs_data:
                    name = spec.get("name", "Unknown")
                    gpu_count = spec.get("gpu_count", "?")
                    cpu = spec.get("cpu_cores", "?")
                    mem = spec.get("memory_gb", "?")
                    click.echo(f"  \u2022 {name}: {gpu_count}x GPU, {cpu} CPU, {mem}GB RAM")
            else:
                click.echo("\u26a0\ufe0f  No specs available for this GPU type")

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
