"""Resources specs command - view cached workspace resource specs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.config import Config
from inspire.platform.openapi.workspace_specs import (
    fetch_workspace_specs,
    load_specs_from_config,
    save_specs_to_config,
)
from inspire.platform.web.session import get_web_session

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _format_specs(specs: list) -> str:
    """Format specs as a table."""
    lines = [f"  {'Spec ID':<38} {'GPU':<8} {'Count':<6} {'CPU':<6} {'Memory':<10} Description"]
    lines.append("  " + "-" * 116)
    for spec in specs:
        gpu_type = spec.gpu_type.value if hasattr(spec.gpu_type, "value") else str(spec.gpu_type)
        spec_id_short = spec.spec_id[:36] + ".." if len(spec.spec_id) > 38 else spec.spec_id
        desc = spec.description[:38] if len(spec.description) > 38 else spec.description
        lines.append(
            f"  {spec_id_short:<38} {gpu_type:<8} {spec.gpu_count:<6} "
            f"{spec.cpu_cores:<6} {spec.memory_gb}GiB{'':<4} {desc}"
        )
    return "\n".join(lines)


def _specs_to_dict(specs: list) -> list[dict]:
    """Convert specs to dict for JSON output."""
    return [
        {
            "spec_id": spec.spec_id,
            "gpu_type": (
                spec.gpu_type.value if hasattr(spec.gpu_type, "value") else str(spec.gpu_type)
            ),
            "gpu_count": spec.gpu_count,
            "cpu_cores": spec.cpu_cores,
            "memory_gb": spec.memory_gb,
            "gpu_memory_gb": spec.gpu_memory_gb,
            "description": spec.description,
        }
        for spec in specs
    ]


@click.command("specs")
@click.option("--workspace-id", help="Filter to specific workspace ID")
@click.option("--refresh", is_flag=True, help="Force re-probe from browser API")
@json_option
@pass_context
def show_specs(ctx: Context, workspace_id: str | None, refresh: bool, json_output: bool) -> None:
    """View cached resource specs for training jobs.

    Examples:
        inspire resources specs                       # Show all cached workspaces
        inspire resources specs --workspace-id ws-xxx # Show specific workspace
        inspire resources specs --refresh             # Re-probe and update cache
    """
    json_output = resolve_json_output(ctx, json_output)

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
    except Exception:
        config = Config.from_env()

    if not config.workspace_specs and not refresh:
        if json_output:
            click.echo(json_formatter.format_json({"workspaces": {}}))
        else:
            click.echo("No resource specs cached. Run 'inspire init --discover' to discover specs.")
        return

    if refresh:
        # Get workspace to refresh
        ws_id = workspace_id or config.job_workspace_id or config.default_workspace_id
        if not ws_id:
            try:
                session = get_web_session(require_workspace=False)
                ws_id = session.workspace_id
            except Exception:
                pass

        if not ws_id:
            exit_with_error(
                ctx,
                error_type="ConfigError",
                message="No workspace ID found. Use --workspace-id to specify.",
                exit_code=EXIT_CONFIG_ERROR,
            )
            return

        try:
            specs = fetch_workspace_specs(ws_id)
            save_specs_to_config(config, ws_id, specs)

            if json_output:
                refresh_output = {
                    "workspace_id": ws_id,
                    "source": "refreshed",
                    "specs": _specs_to_dict(specs),
                }
                click.echo(json_formatter.format_json(refresh_output))
            else:
                click.echo(f"\n📋 Refreshed specs for workspace: {ws_id}")
                click.echo(_format_specs(specs))
                click.echo(f"\nTotal: {len(specs)} specs\n")
            return
        except RuntimeError as e:
            exit_with_error(
                ctx,
                error_type="APIError",
                message=f"Failed to refresh specs: {e}",
                exit_code=EXIT_API_ERROR,
            )
            return

    # Show cached specs. config.workspace_specs holds raw TOML dicts, while the
    # formatters need ResourceSpec objects, so normalize before rendering.
    workspaces_to_show: dict[str, list] = {}
    unparseable: list[str] = []
    if workspace_id:
        cached = load_specs_from_config(config, workspace_id) or []
        if cached:
            workspaces_to_show = {workspace_id: cached}
        else:
            if config.workspace_specs.get(workspace_id):
                unparseable.append(workspace_id)
            # Try to fetch
            try:
                specs = fetch_workspace_specs(workspace_id)
                save_specs_to_config(config, workspace_id, specs)
                workspaces_to_show = {workspace_id: specs}
            except RuntimeError as e:
                exit_with_error(
                    ctx,
                    error_type="APIError",
                    message=f"Failed to get specs: {e}",
                    exit_code=EXIT_API_ERROR,
                )
                return
    else:
        for cached_ws_id in config.workspace_specs:
            cached = load_specs_from_config(config, cached_ws_id) or []
            if cached:
                workspaces_to_show[cached_ws_id] = cached
            else:
                unparseable.append(cached_ws_id)

    if json_output:
        output: dict[str, Any] = {
            "workspaces": {
                ws_id: _specs_to_dict(specs) for ws_id, specs in workspaces_to_show.items()
            },
            "unparseable_workspaces": unparseable,
        }
        click.echo(json_formatter.format_json(output))
    else:
        if not workspaces_to_show:
            click.echo("No specs to display.")
            if unparseable:
                click.echo(
                    "Cached entries could not be parsed (unknown gpu_type or missing fields). "
                    "Run: inspire resources specs --refresh"
                )
            return

        if unparseable:
            click.echo(
                click.style(
                    f"Warning: {len(unparseable)} cached workspace(s) have unreadable specs; "
                    "run 'inspire resources specs --refresh' to re-probe.",
                    fg="yellow",
                )
            )

        lines = ["", "📋 Cached Resource Specs", ""]
        for ws_id, specs in workspaces_to_show.items():
            name = config.workspace_names.get(ws_id, "")
            display = name or ws_id
            lines.append(f"Workspace: {display} ({ws_id})")
            lines.append(_format_specs(specs))
            lines.append(f"Total: {len(specs)} specs\n")

        lines.append("💡 Usage:")
        lines.append("  inspire resources specs --workspace-id <id>  # View specific workspace")
        lines.append("  inspire resources specs --refresh            # Refresh specs")
        click.echo("\n".join(lines))
