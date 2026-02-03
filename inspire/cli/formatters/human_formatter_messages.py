"""Message helpers for human-friendly CLI output."""

from __future__ import annotations

import sys
from typing import Optional


def format_error(message: str, hint: Optional[str] = None) -> str:
    """Format an error message.

    Args:
        message: Error message
        hint: Optional hint for fixing

    Returns:
        Formatted error string
    """
    lines = [f"\n\u274c Error: {message}"]
    if hint:
        lines.append(f"\U0001f4a1 Hint: {hint}")
    return "\n".join(lines)


def format_success(message: str) -> str:
    """Format a success message.

    Args:
        message: Success message

    Returns:
        Formatted success string
    """
    return f"\u2705 {message}"


def format_warning(message: str) -> str:
    """Format a warning message.

    Args:
        message: Warning message

    Returns:
        Formatted warning string
    """
    return f"\u26a0\ufe0f {message}"


def print_error(message: str, hint: Optional[str] = None) -> None:
    """Print an error message to stderr."""
    print(format_error(message, hint), file=sys.stderr)


def print_success(message: str) -> None:
    """Print a success message to stdout."""
    print(format_success(message))
