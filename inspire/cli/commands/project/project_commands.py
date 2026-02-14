"""Project subcommands."""

from __future__ import annotations

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    require_web_session,
    resolve_json_output,
)
from inspire.platform.web import browser_api as browser_api_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_to_dict(proj: browser_api_module.ProjectInfo) -> dict:
    """Convert a ProjectInfo to a plain dict for JSON output."""
    return {
        "project_id": proj.project_id,
        "name": proj.name,
        "workspace_id": proj.workspace_id,
        "budget": proj.budget,
        "remain_budget": proj.remain_budget,
        "member_remain_budget": proj.member_remain_budget,
        "gpu_limit": proj.gpu_limit,
        "member_gpu_limit": proj.member_gpu_limit,
        "priority_level": proj.priority_level,
        "priority_name": proj.priority_name,
    }


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@click.command("list")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def list_projects_cmd(
    ctx: Context,
    json_output: bool,
) -> None:
    """List projects and their GPU quota.

    \b
    Examples:
        inspire project list          # Show project quota table
        inspire project list --json   # JSON output with all fields
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Listing projects requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    try:
        projects = browser_api_module.list_projects(session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to list projects: {e}", EXIT_API_ERROR)
        return

    results = [_project_to_dict(p) for p in projects]

    if json_output:
        click.echo(json_formatter.format_json({"projects": results, "total": len(results)}))
        return

    click.echo(human_formatter.format_project_list(results))
