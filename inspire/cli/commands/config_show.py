"""`inspire config show` implementation."""

from __future__ import annotations

import click

from inspire.cli.commands.config_show_render import _show_env, _show_json, _show_table
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error


@click.command("show")
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["table", "json", "env"]),
    default="table",
    help="Output format (table, json, env)",
)
@click.option(
    "--compact",
    "-c",
    is_flag=True,
    help="Hide unset options",
)
@click.option(
    "--filter",
    "-F",
    "filter_category",
    help="Filter by category (e.g., 'API', 'Gitea')",
)
@pass_context
def show_config(
    ctx: Context, output_format: str, compact: bool, filter_category: str | None
) -> None:
    """Display merged configuration with value sources.

    Shows configuration values from all sources (defaults, global config,
    project config, environment variables) with clear indication of where
    each value comes from.

    By default, all options are shown including unset ones. Use --compact
    to hide unset options.

    \b
    Examples:
        inspire config show
        inspire config show --format json
        inspire config show --filter API
        inspire config show --compact
    """
    try:
        # Load config with source tracking (don't require credentials for show)
        cfg, sources = Config.from_files_and_env(
            require_credentials=False, require_target_dir=False
        )

        # Get config file paths
        global_path, project_path = Config.get_config_paths()

        if output_format == "json":
            _show_json(cfg, sources, global_path, project_path, compact, filter_category)
        elif output_format == "env":
            _show_env(cfg, compact, filter_category)
        else:
            _show_table(cfg, sources, global_path, project_path, compact, filter_category)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
