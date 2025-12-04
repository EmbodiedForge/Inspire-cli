"""Node commands for Inspire CLI.

Commands:
    inspire nodes list - List cluster nodes
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
def nodes():
    """View cluster nodes."""
    pass


@nodes.command("list")
@click.option("--pool", type=click.Choice(["online", "backup", "fault", "unknown"]),
              help="Filter by resource pool")
@click.option("--page", type=int, default=1, help="Page number (default: 1)")
@click.option("--size", type=int, default=20, help="Page size (default: 20)")
@pass_context
def list_nodes(ctx: Context, pool: str, page: int, size: int):
    """List cluster nodes.

    Shows available nodes in the cluster, optionally filtered by resource pool.

    \b
    Examples:
        inspire nodes list
        inspire nodes list --pool online
        inspire nodes list --pool fault --size 50
    """
    try:
        config = Config.from_env()
        api = AuthManager.get_api(config)

        result = api.list_cluster_nodes(
            page_num=page,
            page_size=size,
            resource_pool=pool,
        )

        nodes_data = result.get("data", {}).get("nodes", [])
        total = result.get("data", {}).get("total", len(nodes_data))

        if ctx.json_output:
            click.echo(json_formatter.format_json({
                "nodes": nodes_data,
                "total": total,
                "page": page,
                "page_size": size,
                "pool_filter": pool,
            }))
        else:
            click.echo(human_formatter.format_nodes(nodes_data, total))

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
