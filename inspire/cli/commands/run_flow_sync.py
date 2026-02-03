"""Sync helpers for `inspire run`."""

from __future__ import annotations

import time

import click

from inspire.cli.commands.run_helpers import _check_uncommitted_changes, _run_inspire_subcommand
from inspire.cli.context import Context, EXIT_GENERAL_ERROR, EXIT_SUCCESS
from inspire.cli.utils.errors import exit_with_error as _handle_error


def run_sync_if_requested(ctx: Context, *, sync: bool, watch: bool) -> None:
    """Run `inspire sync` if `--sync` or `--watch` are set (may sys.exit on error)."""
    if not (sync or watch):
        return

    if not ctx.json_output:
        click.echo("Syncing code...")

    # Check for uncommitted changes (avoid sync interactive prompts)
    if _check_uncommitted_changes():
        _handle_error(
            ctx,
            "ValidationError",
            "Uncommitted changes detected. Commit or stash first.",
            EXIT_GENERAL_ERROR,
        )

    try:
        exit_code = _run_inspire_subcommand(["sync"])
    except Exception as e:
        _handle_error(ctx, "SyncError", f"Failed to run sync: {e}", EXIT_GENERAL_ERROR)

    if exit_code != EXIT_SUCCESS:
        _handle_error(ctx, "SyncError", "Code sync failed", EXIT_GENERAL_ERROR)

    # Brief delay after sync to avoid API rate limits
    time.sleep(0.5)


__all__ = ["run_sync_if_requested"]
