"""Notebook status command."""

from __future__ import annotations

import click

from inspire.cli.commands.notebook_common import (
    _get_base_url,
    _require_web_session,
    _resolve_json_output,
)
from inspire.cli.context import Context, EXIT_API_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import web_session as web_session_module
from inspire.cli.utils.errors import exit_with_error as _handle_error


@click.command("status")
@click.argument("instance_id")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def notebook_status(
    ctx: Context,
    instance_id: str,
    json_output: bool,
) -> None:
    """Get status of a notebook instance.

    \b
    Examples:
        inspire notebook status notebook-abc-123
    """
    json_output = _resolve_json_output(ctx, json_output)

    session = _require_web_session(
        ctx,
        hint=(
            "Notebook status requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

    base_url = _get_base_url()

    try:
        data = web_session_module.request_json(
            session,
            "GET",
            f"{base_url}/api/v1/notebook/{instance_id}",
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except ValueError as e:
        message = str(e)
        if "API returned 404" in message:
            _handle_error(
                ctx,
                "NotFound",
                f"Notebook instance '{instance_id}' not found",
                EXIT_API_ERROR,
            )
        else:
            _handle_error(ctx, "APIError", message, EXIT_API_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if data.get("code") == 0:
        notebook = data.get("data", {})
        if json_output:
            click.echo(json_formatter.format_json(notebook))
        else:
            _print_notebook_detail(notebook)
        return

    _handle_error(
        ctx,
        "APIError",
        data.get("message", "Unknown error"),
        EXIT_API_ERROR,
    )
    return


def _print_notebook_detail(notebook: dict) -> None:
    """Print detailed notebook information."""
    click.echo(f"\n{'='*60}")
    click.echo(f"Notebook: {notebook.get('name', 'N/A')}")
    click.echo(f"{'='*60}")

    fields = [
        ("ID", notebook.get("id")),
        ("Status", notebook.get("status")),
        ("Project", notebook.get("project_name")),
        ("Created", notebook.get("created_at")),
    ]

    # Resource spec
    if "resource_spec" in notebook:
        spec = notebook["resource_spec"]
        fields.extend(
            [
                ("GPU Count", spec.get("gpu_count")),
                ("GPU Type", spec.get("gpu_type")),
                ("CPU", spec.get("cpu_count")),
                ("Memory", spec.get("memory_size")),
            ]
        )

    for label, value in fields:
        if value:
            click.echo(f"  {label:<15}: {value}")

    click.echo(f"{'='*60}\n")
