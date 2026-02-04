"""Workspace-scoped availability mode for `inspire resources list`."""

from __future__ import annotations

import click

from inspire.cli.commands.resources_list_format import _format_availability_table
from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_AUTH_ERROR
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.config import Config
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.resources import clear_availability_cache, fetch_resource_availability
from inspire.cli.utils.web_session import SessionExpiredError


def _list_workspace_resources(ctx: Context, show_all: bool, no_cache: bool) -> None:
    """List workspace-specific GPU availability using browser API."""
    try:
        if no_cache:
            clear_availability_cache()

        config = None
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
        except Exception:
            pass

        availability = fetch_resource_availability(
            config=config,
            known_only=not show_all,
        )

        if not availability:
            click.echo(human_formatter.format_error("No GPU resources found in your workspace"))
            return

        if ctx.json_output:
            output = [
                {
                    "group_id": a.group_id,
                    "group_name": a.group_name,
                    "gpu_type": a.gpu_type,
                    "gpus_per_node": a.gpu_per_node,
                    "total_nodes": a.total_nodes,
                    "ready_nodes": a.ready_nodes,
                    "free_nodes": a.free_nodes,
                    "free_gpus": a.free_gpus,
                }
                for a in availability
            ]
            click.echo(json_formatter.format_json({"availability": output}))
            return

        _format_availability_table(availability, workspace_mode=True)

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
