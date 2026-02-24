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

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"


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


def _unique_workspace_ids(values: list[str | None]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        ws_id = str(value or "").strip()
        if not ws_id or ws_id == _ZERO_WORKSPACE_ID:
            continue
        if ws_id in seen:
            continue
        seen.add(ws_id)
        unique.append(ws_id)
    return unique


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
        workspace_ids = session.all_workspace_ids
        if workspace_ids is None:
            # Workspace discovery never happened (stale session or login
            # method that doesn't support it).  Try using workspace IDs
            # from the config; if none are configured, fall back to the
            # session's single workspace_id.
            from inspire.config import Config as _Cfg

            try:
                _cfg, _ = _Cfg.from_files_and_env(
                    require_credentials=False, require_target_dir=False
                )
                cfg_candidates = [
                    getattr(_cfg, "workspace_cpu_id", None),
                    getattr(_cfg, "workspace_gpu_id", None),
                    getattr(_cfg, "workspace_internet_id", None),
                ]
                cfg_workspaces = getattr(_cfg, "workspaces", None)
                if isinstance(cfg_workspaces, dict):
                    cfg_candidates.extend(cfg_workspaces.values())
                cfg_ws = _unique_workspace_ids(cfg_candidates)
            except Exception:
                cfg_ws = []
            workspace_ids = cfg_ws or _unique_workspace_ids(
                [getattr(session, "workspace_id", None)]
            )
        else:
            workspace_ids = _unique_workspace_ids(list(workspace_ids))
        if not workspace_ids:
            # No discovered workspaces — fall back to default query
            projects = browser_api_module.list_projects(session=session)
        else:
            seen: set[str] = set()
            projects = []
            workspace_errors: list[tuple[str, str]] = []
            for ws_id in workspace_ids:
                try:
                    ws_projects = browser_api_module.list_projects(
                        workspace_id=ws_id, session=session
                    )
                except Exception as e:
                    workspace_errors.append((ws_id, str(e)))
                    continue
                for p in ws_projects:
                    if p.project_id not in seen:
                        seen.add(p.project_id)
                        projects.append(p)
            if not projects and workspace_errors:
                try:
                    projects = browser_api_module.list_projects(session=session)
                except Exception as e:
                    error_samples = ", ".join(
                        f"{ws_id}: {message}" for ws_id, message in workspace_errors[:3]
                    )
                    if len(workspace_errors) > 3:
                        error_samples += ", ..."
                    raise ValueError(
                        f"Failed to list projects across configured workspaces "
                        f"({len(workspace_errors)} failed: {error_samples}); "
                        f"default query failed: {e}"
                    ) from e
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to list projects: {e}", EXIT_API_ERROR)
        return

    results = [_project_to_dict(p) for p in projects]

    if json_output:
        click.echo(json_formatter.format_json({"projects": results, "total": len(results)}))
        return

    click.echo(human_formatter.format_project_list(results))
