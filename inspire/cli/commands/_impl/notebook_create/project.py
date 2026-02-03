"""Project selection for `inspire notebook create`."""

from __future__ import annotations

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error


def resolve_notebook_project(
    ctx: Context,
    *,
    projects: list,
    project: str | None,
    json_output: bool,
) -> object | None:
    """Select project from the list. Returns ProjectInfo or None on handled error."""
    try:
        selected_project, fallback_msg = browser_api_module.select_project(projects, project)

        if not json_output:
            if fallback_msg:
                click.echo(fallback_msg)
            click.echo(
                f"Using project: {selected_project.name}{selected_project.get_quota_status()}"
            )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            hint = None
            if projects:
                hint = "Available projects:\n" + "\n".join(f"  - {p.name}" for p in projects)
            _handle_error(ctx, "ValidationError", error_msg, EXIT_CONFIG_ERROR, hint=hint)
            return None
        _handle_error(ctx, "QuotaExceeded", error_msg, EXIT_CONFIG_ERROR)
        return None

    return selected_project


__all__ = ["resolve_notebook_project"]
