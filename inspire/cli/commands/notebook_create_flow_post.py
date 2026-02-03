"""Post-create actions for `inspire notebook create`."""

from __future__ import annotations

import click

from inspire.cli.context import Context, EXIT_API_ERROR
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.web_session import WebSession


def maybe_wait_for_running(
    ctx: Context,
    *,
    notebook_id: str,
    session: WebSession,
    wait: bool,
    keepalive: bool,
    json_output: bool,
    timeout: int = 600,
) -> bool:
    if not (wait or keepalive):
        return True

    if not json_output:
        click.echo("Waiting for notebook to reach RUNNING status...")

    try:
        browser_api_module.wait_for_notebook_running(
            notebook_id=notebook_id,
            session=session,
            timeout=timeout,
        )
        if not json_output:
            click.echo("Notebook is now RUNNING.")
        return True
    except TimeoutError as e:
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook to reach RUNNING: {e}",
            EXIT_API_ERROR,
        )
        return False


def maybe_start_keepalive(
    ctx: Context,
    *,
    notebook_id: str,
    session: WebSession,
    keepalive: bool,
    gpu_count: int,
    json_output: bool,
) -> None:
    if not (keepalive and gpu_count > 0):
        return

    from inspire.cli.utils.keepalive import get_keepalive_command

    if not json_output:
        click.echo("Starting GPU keepalive script...")

    try:
        browser_api_module.run_command_in_notebook(
            notebook_id=notebook_id,
            command=get_keepalive_command(),
            session=session,
        )
        if not json_output:
            click.echo("GPU keepalive script started (log: /tmp/keepalive.log)")
    except Exception as e:
        if not json_output:
            click.echo(f"Warning: Failed to start keepalive script: {e}", err=True)


__all__ = ["maybe_start_keepalive", "maybe_wait_for_running"]
