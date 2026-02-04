"""Init command flow for Inspire CLI.

Kept separate so `init_helpers.py` can remain a thin façade.
"""

from __future__ import annotations

from pathlib import Path

import click

from inspire.cli.commands.init_env import _detect_env_vars
from inspire.cli.commands.init_preview import _format_preview_by_scope
from inspire.cli.commands.init_template import _init_template_mode
from inspire.cli.commands.init_write import _write_auto_split, _write_single_file
from inspire.config import CONFIG_FILENAME, PROJECT_CONFIG_DIR, Config, ConfigOption


def _init_smart_mode(
    detected: list[tuple[ConfigOption, str]],
    global_flag: bool,
    project_flag: bool,
    force: bool,
) -> None:
    """Initialize config using detected env vars (smart mode).

    Auto-splits by scope unless --global or --project is specified.
    Secrets are always excluded.
    """
    # Show preview grouped by scope
    _format_preview_by_scope(detected)

    # Count values and scopes
    secrets = [opt for opt, _ in detected if opt.secret]
    non_secrets = [(opt, val) for opt, val in detected if not opt.secret]
    global_opts = [(opt, val) for opt, val in detected if opt.scope == "global"]
    project_opts = [(opt, val) for opt, val in detected if opt.scope == "project"]

    click.echo(f"Found {len(detected)} environment variable(s):")
    click.echo(f"  - {len(non_secrets)} regular value(s)")
    if secrets:
        click.echo(f"  - {len(secrets)} secret(s) (excluded)")
    if not global_flag and not project_flag:
        click.echo(f"  - {len(global_opts)} global-scope option(s)")
        click.echo(f"  - {len(project_opts)} project-scope option(s)")
    click.echo()

    # Define paths
    global_path = Config.GLOBAL_CONFIG_PATH
    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME

    # Handle different modes
    if global_flag:
        # Force all to global
        _write_single_file(detected, global_path, force, "global")
    elif project_flag:
        # Force all to project
        _write_single_file(detected, project_path, force, "project")
    else:
        # Auto-split by scope
        _write_auto_split(
            detected, global_opts, project_opts, global_path, project_path, force, secrets
        )


@click.command()
@click.option(
    "--global",
    "-g",
    "global_flag",
    is_flag=True,
    help="Force all options to global config (~/.config/inspire/)",
)
@click.option(
    "--project",
    "-p",
    "project_flag",
    is_flag=True,
    help="Force all options to project config (./.inspire/)",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing files without prompting",
)
@click.option(
    "--template",
    "-t",
    "template_flag",
    is_flag=True,
    help="Create template with placeholders (skip env var detection)",
)
def init(global_flag: bool, project_flag: bool, force: bool, template_flag: bool) -> None:
    """Initialize Inspire CLI configuration.

    Detects environment variables and creates config.toml files.
    By default, options are auto-split by scope: global options go to
    ~/.config/inspire/config.toml, project options go to ./.inspire/config.toml.

    Use --global or --project to force all options to a single file.
    Secrets (passwords, tokens) are never written to config files for security.

    If no environment variables are detected (or with --template), creates
    a template config with placeholder values.

    \b
    Examples:
        # Auto-detect env vars and split by scope
        inspire init

        \b
        # Force all options to global config
        inspire init --global

        \b
        # Force all options to project config
        inspire init --project

        \b
        # Create template with placeholders
        inspire init --template
    """
    # Validate flags
    if global_flag and project_flag:
        click.echo(click.style("Error: Cannot specify both --global and --project", fg="red"))
        raise SystemExit(1)

    # Template mode - skip env var detection
    if template_flag:
        click.echo("Creating template config with placeholders.\n")
        _init_template_mode(global_flag, project_flag, force)
        return

    # Detect environment variables
    detected = _detect_env_vars()

    if detected:
        # Smart mode - use detected env vars
        _init_smart_mode(detected, global_flag, project_flag, force)
    else:
        # No env vars - fall back to template mode
        click.echo("No environment variables detected. Creating template config.\n")
        _init_template_mode(global_flag, project_flag, force)
