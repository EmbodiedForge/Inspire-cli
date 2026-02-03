"""Accurate (real-time) availability mode for `inspire resources list`."""

from __future__ import annotations

import click

from inspire.cli.commands.resources_list_format import _format_accurate_availability_table
from inspire.cli.commands.resources_list_known_groups import _known_compute_groups_from_config
from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_AUTH_ERROR
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.web_session import SessionExpiredError


def _list_accurate_resources(ctx: Context, show_all: bool) -> None:
    """List accurate GPU availability using browser API."""
    try:
        known_groups = _known_compute_groups_from_config(show_all=show_all)

        availability = browser_api_module.get_accurate_gpu_availability()

        if not show_all:
            availability = [a for a in availability if a.group_id in known_groups]
            for entry in availability:
                if not entry.group_name:
                    entry.group_name = known_groups.get(entry.group_id, entry.group_name)

        if not availability:
            if ctx.json_output:
                click.echo(json_formatter.format_json({"availability": []}))
            else:
                click.echo(human_formatter.format_error("No GPU resources found"))
            return

        if ctx.json_output:
            output = [
                {
                    "group_id": a.group_id,
                    "group_name": a.group_name,
                    "gpu_type": a.gpu_type,
                    "total_gpus": a.total_gpus,
                    "used_gpus": a.used_gpus,
                    "available_gpus": a.available_gpus,
                    "low_priority_gpus": a.low_priority_gpus,
                }
                for a in availability
            ]
            click.echo(json_formatter.format_json({"availability": output}))
        else:
            _format_accurate_availability_table(availability)

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
