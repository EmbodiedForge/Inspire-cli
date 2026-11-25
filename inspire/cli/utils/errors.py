"""Shared CLI error handling utilities.

Centralizes JSON vs human formatting and consistent exit codes across commands.

This module wraps the core output utilities from `inspire.cli.utils.output`
to provide both simple error emission (non-exiting) and immediate exit patterns.

Usage:
    from inspire.cli.utils.errors import exit_with_error as _handle_error

    # Simple error (exits immediately):
    _handle_error(ctx, "ErrorType", "Message", EXIT_CODE)

    # Error with hint:
    _handle_error(ctx, "ErrorType", "Message", EXIT_CODE, hint="Try this...")

For multi-line output or success messages, use output.py directly:
    from inspire.cli.utils.output import emit_error, emit_success

    emit_error(ctx, error_type="Type", message="msg", exit_code=1, hint="hint")
    emit_success(ctx, payload={...}, text="Operation completed")
"""

from __future__ import annotations

import sys
from typing import Any, NoReturn

from inspire.cli.context import EXIT_GENERAL_ERROR, Context
from inspire.cli.utils.output import (
    emit_error as _emit_error_core,
)


def emit_error(
    ctx: Context,
    error_type: str,
    message: str,
    exit_code: int = EXIT_GENERAL_ERROR,
    *,
    hint: str | None = None,
    data: Any | None = None,
) -> int:
    """Emit a formatted error without exiting. Returns exit_code.

    All errors go to stderr in both JSON and human modes.
    Human mode: outputs 'Error: {message}' + optional hint.
    JSON mode: outputs structured error object.

    Args:
        ctx: CLI context with json_output flag
        error_type: Type/classification of error (e.g., "ConfigError")
        message: Human-readable error message
        exit_code: Exit code for the error
        hint: Optional hint for fixing the error

    Returns:
        The exit_code that was passed in
    """
    _emit_error_core(
        ctx,
        error_type=error_type,
        message=message,
        exit_code=exit_code,
        hint=hint,
        data=data,
    )
    return exit_code


def exit_with_error(
    ctx: Context,
    error_type: str,
    message: str,
    exit_code: int = EXIT_GENERAL_ERROR,
    *,
    hint: str | None = None,
    data: Any | None = None,
) -> NoReturn:
    """Print a formatted error and exit with the given code.

    This is a convenience wrapper that calls emit_error() then sys.exit().

    Args:
        ctx: CLI context
        error_type: Type of error
        message: Error message
        exit_code: Exit code to use
        hint: Optional hint
    """
    emit_error(
        ctx,
        error_type=error_type,
        message=message,
        exit_code=exit_code,
        hint=hint,
        data=data,
    )
    sys.exit(exit_code)


__all__ = ["emit_error", "exit_with_error"]
