"""Notebook lifecycle commands (start/stop)."""

from __future__ import annotations

import click

from inspire.cli.context import Context, EXIT_API_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import require_web_session, resolve_json_output


@click.command("stop")
@click.argument("notebook_id")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def stop_notebook_cmd(
    ctx: Context,
    notebook_id: str,
    json_output: bool,
) -> None:
    """Stop a running notebook instance.

    \b
    Examples:
        inspire notebook stop abc123-def456
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Stopping notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

    try:
        result = browser_api_module.stop_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to stop notebook: {e}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook_id": notebook_id,
                    "status": "stopping",
                    "result": result,
                }
            )
        )
        return

    click.echo(f"Notebook '{notebook_id}' is being stopped.")
    click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")


@click.command("start")
@click.argument("notebook_id")
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for notebook to reach RUNNING status",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def start_notebook_cmd(
    ctx: Context,
    notebook_id: str,
    wait: bool,
    json_output: bool,
) -> None:
    """Start a stopped notebook instance.

    \b
    Examples:
        inspire notebook start abc123-def456
        inspire notebook start abc123-def456 --wait
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Starting notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

    try:
        result = browser_api_module.start_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to start notebook: {e}", EXIT_API_ERROR)
        return

    if not json_output:
        click.echo(f"Notebook '{notebook_id}' is being started.")

    if wait:
        if not json_output:
            click.echo("Waiting for notebook to reach RUNNING status...")
        try:
            browser_api_module.wait_for_notebook_running(notebook_id=notebook_id, session=session)
            if not json_output:
                click.echo("Notebook is now RUNNING.")
        except TimeoutError as e:
            _handle_error(
                ctx,
                "Timeout",
                f"Timed out waiting for notebook to reach RUNNING: {e}",
                EXIT_API_ERROR,
            )
            return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook_id": notebook_id,
                    "status": "starting",
                    "result": result,
                }
            )
        )
        return

    click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")
