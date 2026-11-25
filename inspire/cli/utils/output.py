"""Shared helpers for emitting CLI output in JSON and human modes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import click

from inspire.cli.context import Context
from inspire.cli.formatters import json_formatter


def _emit_debug_report_hint(ctx: Context) -> None:
    """Emit debug report path hint if in debug mode."""
    if not getattr(ctx, "debug", False):
        return
    debug_report_path = getattr(ctx, "debug_report_path", None)
    if not debug_report_path:
        return
    click.echo(f"Debug report: {debug_report_path}", err=True)


def emit_success(
    ctx: Context,
    *,
    payload: dict[str, Any],
    text: str | None = None,
) -> None:
    """Emit a success message.

    In JSON mode: outputs structured payload to stdout.
    In human mode: outputs 'OK {text}' to stdout.

    Args:
        ctx: CLI context with json_output flag
        payload: Structured data for JSON mode (always includes success: True)
        text: Human-readable message (default: "OK" if not provided)
    """
    if ctx.json_output:
        # format_json already wraps with {"success": True, "data": ...}
        click.echo(json_formatter.format_json(payload))
        return

    human_text = text if text is not None else "OK"
    # Always prefix with "OK" for consistency
    if not human_text.startswith("OK"):
        human_text = f"OK {human_text}"
    click.echo(human_text)


def emit_error(
    ctx: Context,
    *,
    error_type: str,
    message: str,
    exit_code: int,
    hint: str | None = None,
    human_lines: Iterable[str] | None = None,
    data: Any | None = None,
) -> None:
    """Emit an error message.

    In JSON mode: outputs structured error to stderr.
    In human mode: outputs 'Error: {message}' + hint to stderr,
    or uses provided human_lines for multi-line output.

    Args:
        ctx: CLI context with json_output flag
        error_type: Type/classification of error (e.g., "ConfigError")
        message: Human-readable error message
        exit_code: Exit code for the error
        hint: Optional hint for fixing the error
        human_lines: Optional multi-line human output (takes precedence in human mode)
    """
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(error_type, message, exit_code, hint=hint, data=data),
            err=True,
        )
        return

    # Human mode: always output to stderr
    click.echo(f"Error: {message}", err=True)
    if hint:
        click.echo(f"Hint: {hint}", err=True)
    if human_lines is not None:
        for line in human_lines:
            click.echo(line, err=True)
    _emit_debug_report_hint(ctx)


def emit_warning(ctx: Context, message: str) -> None:
    """Emit a warning message to stderr.

    Warnings are non-fatal issues that don't prevent operation
    but should be noted by the user.

    Args:
        ctx: CLI context
        message: Warning message text
    """
    if ctx.json_output:
        # In JSON mode, warnings are structured data
        click.echo(
            json_formatter.format_json({"warning": message}),
            err=True,
        )
        return

    click.echo(f"Warning: {message}", err=True)


def emit_info(ctx: Context, message: str) -> None:
    """Emit informational/debug message to stderr.

    Info messages are diagnostic output that should not be
    part of the primary command output.

    Args:
        ctx: CLI context
        message: Info message text
    """
    if ctx.json_output:
        # Machine mode keeps stdout to exactly one JSON document. Info lines are
        # diagnostics, so they stay plain text on stderr. Wrapping them in a JSON
        # envelope and sending it to stderr meant `cmd --json | jq` read an empty
        # stdout and found nothing, while the "JSON" nobody asked for went unread.
        click.echo(message, err=True)
        return

    # Only show in debug mode or if explicitly verbose
    if getattr(ctx, "debug", False) or getattr(ctx, "verbose", False):
        click.echo(message, err=True)


def emit_progress(ctx: Context, message: str) -> None:
    """Emit a progress/status update to stderr.

    Progress messages indicate ongoing operations.

    Args:
        ctx: CLI context
        message: Progress message text
    """
    if ctx.json_output:
        # Suppress progress in JSON mode
        return

    click.echo(message, err=True)


__all__ = [
    "emit_success",
    "emit_error",
    "emit_warning",
    "emit_info",
    "emit_progress",
]
