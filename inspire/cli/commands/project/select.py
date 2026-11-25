"""Project select command – interactive project priority selector."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import questionary
import tomlkit

from inspire.cli.utils import project_order as project_order_utils
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success
from inspire.config import (
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    Config,
    ConfigError,
    PROJECT_CONFIG_DIR,
)
from inspire.config.toml import _find_project_config


def _effective_project_order_layer(config: Config) -> tuple[list[str], str, str | None]:
    """Return the effective project_order with the layer and file providing it."""
    sources = getattr(config, "_sources", {}) or {}
    source = str(sources.get("project_order") or "unknown")
    if source == SOURCE_PROJECT:
        path = getattr(config, "_project_config_path", None)
    elif source == SOURCE_GLOBAL:
        path = getattr(config, "_global_config_path", None)
    else:
        path = None
    order = [str(entry) for entry in (getattr(config, "project_order", None) or [])]
    return order, source, (str(path) if path else None)


def _project_order_layer_label(source: str, config_path: str | None) -> str:
    if source == SOURCE_ENV:
        return "environment variable INSPIRE_PROJECT_ORDER"
    if config_path:
        return f"{source} config ({config_path})"
    return f"{source} configuration"


def _build_project_indexes(
    available_projects: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, int]]:
    """Build lookup tables for project IDs and unique names."""
    projects_by_id = {str(p.get("id") or ""): p for p in available_projects}
    name_counts: dict[str, int] = {}
    for p in available_projects:
        norm = str(p.get("name") or "").strip().casefold()
        if norm:
            name_counts[norm] = name_counts.get(norm, 0) + 1

    projects_by_unique_name = {
        (norm := str(p.get("name") or "").strip().casefold()): p
        for p in available_projects
        if norm and name_counts.get(norm) == 1
    }
    return projects_by_id, projects_by_unique_name, name_counts


def _resolve_project_entry(
    entry: str,
    *,
    projects_by_id: dict[str, dict[str, Any]],
    projects_by_unique_name: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve a project_order entry by ID first, then by unique legacy name."""
    entry = str(entry or "").strip()
    if not entry:
        return None
    if entry in projects_by_id:
        return projects_by_id[entry]
    return projects_by_unique_name.get(entry.casefold())


def _serialize_project_order(ordered_project_ids: list[str]) -> list[str]:
    """Serialize project order as stable project IDs only."""
    return [str(project_id) for project_id in ordered_project_ids if str(project_id).strip()]


def _select_and_order_projects(
    available_projects: list[dict[str, Any]],
    current_order: list[str],
    *,
    projects_by_id: dict[str, dict[str, Any]],
    projects_by_unique_name: dict[str, dict[str, Any]],
) -> list[str] | None:
    """Interactive project selection and ordering.

    Returns list of project IDs in priority order, or None if cancelled.
    """
    checked_ids = {
        str(r.get("id") or "")
        for entry in current_order
        if (
            r := _resolve_project_entry(
                entry,
                projects_by_id=projects_by_id,
                projects_by_unique_name=projects_by_unique_name,
            )
        )
    }

    choices = [
        questionary.Choice(
            title=str(p.get("name") or p.get("id") or "Unknown"),
            value=str(p.get("id") or ""),
            checked=str(p.get("id") or "") in checked_ids,
        )
        for p in available_projects
    ]

    selected = questionary.checkbox(
        "Select projects to include in priority order:\n"
        "(Use Space to select/deselect, Enter to confirm)",
        choices=choices,
    ).ask()

    if selected is None:
        return None
    if not selected:
        return []
    if len(selected) == 1:
        return _serialize_project_order(selected)

    click.echo("\nNow let's set the priority order (highest priority first):\n")

    ordered: list[str] = []
    remaining = selected.copy()

    for i in range(len(selected)):
        if len(remaining) == 1:
            ordered.append(remaining[0])
            break

        remaining_choices = [c for c in choices if c.value in remaining]
        choice = questionary.select(
            f"Priority {i + 1} (highest):",
            choices=remaining_choices,
        ).ask()

        if choice is None:
            return None
        ordered.append(choice)
        remaining.remove(choice)

    return _serialize_project_order(ordered)


def _update_project_config(project_path: Path, project_order: list[str]) -> None:
    """Update the project config with new project_order."""
    try:
        if project_path.exists():
            doc = tomlkit.parse(project_path.read_text(encoding="utf-8"))
        else:
            doc = tomlkit.document()
            doc.add(tomlkit.comment("Inspire CLI Project Configuration"))
            doc.add(tomlkit.nl())

        if "defaults" not in doc:
            doc["defaults"] = tomlkit.table()

        if project_order:
            doc["defaults"]["project_order"] = project_order
        elif "project_order" in doc["defaults"]:
            del doc["defaults"]["project_order"]

        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    except Exception as e:
        raise ConfigError(f"Failed to update project configuration: {e}") from e


@click.command("select")
@click.option(
    "--reset",
    is_flag=True,
    help="Clear project_order (no projects preferred)",
)
@pass_context
def select_projects(
    ctx: Context,
    reset: bool,
) -> None:
    """Interactively select and prioritize projects.

    Launches an interactive picker to select which projects to use and
    set their priority order. The order is saved to defaults.project_order
    in the project config.

    Projects are used in priority order when --project is not specified
    on commands like 'job create'.

    \b
    Examples:
        inspire project select              # Interactive selection
        inspire project select --reset      # Clear project_order
        inspire --json project select       # Show current order as JSON
    """
    effective_json = resolve_json_output(ctx, False)

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)

        project_path = _find_project_config()
        if not project_path:
            project_path = Path.cwd() / PROJECT_CONFIG_DIR / "config.toml"

        if reset:
            _update_project_config(project_path, [])
            refreshed, _ = Config.from_files_and_env(require_credentials=False)
            remaining, source, source_path = _effective_project_order_layer(refreshed)

            if not remaining:
                emit_success(
                    ctx,
                    text="Project order cleared",
                    payload={"project_order": [], "action": "reset"},
                )
                return

            # The project-level key is gone, but another layer still supplies an
            # effective order, so "cleared" would be a false claim.
            warning = (
                "project_order is still effective from "
                f"{_project_order_layer_label(source, source_path)}; "
                "only the project-level value was cleared."
            )
            emit_success(
                ctx,
                text="Project-level project order cleared",
                payload={
                    "project_order": remaining,
                    "action": "reset",
                    "cleared": False,
                    "remaining_source": source,
                    "remaining_config_path": source_path,
                    "warning": warning,
                },
            )
            if not effective_json:
                click.echo(click.style(f"Warning: {warning}", fg="yellow"))
            return

        if effective_json:
            current_order = config.project_order or []
            emit_success(
                ctx,
                payload={
                    "project_order": current_order,
                    "config_path": str(project_path),
                },
            )
            return

        live_projects = project_order_utils.fetch_current_account_projects(config)
        projects = [
            {
                "id": project.project_id,
                "name": project.name,
                "workspace_id": project.workspace_id,
            }
            for project in live_projects
        ]

        if not projects:
            _handle_error(
                ctx,
                "ConfigError",
                "No projects found for the current account.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire project list' to verify current account access.",
            )
            return

        all_have_names = all(
            str(p.get("name", "")).strip() and not str(p.get("name", "")).startswith("project-")
            for p in projects
        )
        if not all_have_names:
            click.echo(
                click.style(
                    "Note: Project names not available. Run 'inspire init --discover' to fetch project names.",
                    fg="yellow",
                )
            )
            click.echo()

        projects_by_id, projects_by_unique_name, _name_counts = _build_project_indexes(projects)

        current_order = config.project_order or []
        if current_order:
            click.echo(click.style("\nCurrent project priority order:", fg="blue"))
            for i, entry in enumerate(current_order, 1):
                resolved = _resolve_project_entry(
                    entry,
                    projects_by_id=projects_by_id,
                    projects_by_unique_name=projects_by_unique_name,
                )
                label = (
                    str(resolved.get("name") or resolved.get("id") or entry) if resolved else entry
                )
                click.echo(f"  {i}. {label}")
            click.echo()
        else:
            click.echo(click.style("\nNo project priority order configured.", fg="yellow"))
            click.echo("Select projects below to set the order.\n")

        new_order = _select_and_order_projects(
            projects,
            current_order,
            projects_by_id=projects_by_id,
            projects_by_unique_name=projects_by_unique_name,
        )

        if new_order is None:
            emit_info(ctx, "Selection cancelled.")
            return

        _update_project_config(project_path, new_order)
        click.echo()
        emit_success(
            ctx,
            text="Project order updated successfully",
            payload={"project_order": new_order},
        )

        click.echo()
        click.echo(click.style("New project priority order:", fg="green"))
        for i, entry in enumerate(new_order, 1):
            resolved = _resolve_project_entry(
                entry,
                projects_by_id=projects_by_id,
                projects_by_unique_name=projects_by_unique_name,
            )
            label = str(resolved.get("name") or resolved.get("id") or entry) if resolved else entry
            click.echo(f"  {i}. {label}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
