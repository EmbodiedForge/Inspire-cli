"""Shared CLI error handling utilities.

Centralizes JSON vs human formatting and consistent exit codes across commands.
"""

from __future__ import annotations

import sys

import click

from inspire.cli.context import Context
from inspire.cli.formatters import human_formatter, json_formatter


def exit_with_error(
    ctx: Context,
    error_type: str,
    message: str,
    exit_code: int,
    *,
    hint: str | None = None,
) -> None:
    """Print a formatted error and exit with the given code."""
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(
                error_type,
                message,
                exit_code,
                hint=hint,
            ),
            err=True,
        )
    else:
        click.echo(human_formatter.format_error(message, hint=hint), err=True)
    sys.exit(exit_code)

