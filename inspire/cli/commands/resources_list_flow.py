"""Resources list flow for `inspire resources list`."""

from __future__ import annotations

import sys

import click

from inspire.cli.commands.resources_list_accurate import _list_accurate_resources
from inspire.cli.commands.resources_list_workspace import _list_workspace_resources
from inspire.cli.commands.resources_list_watch import _watch_resources
from inspire.cli.context import Context, EXIT_CONFIG_ERROR
from inspire.cli.formatters import json_formatter


def run_resources_list(
    ctx: Context,
    *,
    no_cache: bool,
    show_all: bool,
    watch: bool,
    interval: int,
    workspace: bool,
    use_global: bool,
) -> None:
    """Implementation for `inspire resources list`."""
    if watch:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "InvalidOption",
                    "Watch mode not supported with JSON output",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
            sys.exit(EXIT_CONFIG_ERROR)

        _watch_resources(ctx, show_all, interval, workspace, use_global)
        return

    if workspace or use_global:
        if use_global and not workspace:
            click.echo(
                "Note: --global is deprecated; showing workspace node availability instead.",
                err=True,
            )
        _list_workspace_resources(ctx, show_all, no_cache)
        return

    _list_accurate_resources(ctx, show_all)
