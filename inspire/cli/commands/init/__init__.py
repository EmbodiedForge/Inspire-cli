"""Init command helpers.

This module is the stable import surface for the `inspire init` command and its helper
functions used by tests.
"""

from __future__ import annotations

import click

from .discover import _derive_shared_path_group, _init_discover_mode
from .env_detect import _detect_env_vars, _generate_toml_content
from .templates import CONFIG_TEMPLATE, _init_smart_mode, _init_template_mode


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
    "--discover",
    is_flag=True,
    help="Discover projects/workspaces and write per-account catalog",
)
@click.option(
    "--probe-shared-path",
    is_flag=True,
    help=(
        "Probe shared filesystem paths by SSHing into a small CPU notebook per project "
        "(slow; creates notebooks)."
    ),
)
@click.option(
    "--probe-limit",
    type=int,
    default=0,
    show_default=True,
    help="Limit number of projects to probe (0 = all)",
)
@click.option(
    "--probe-keep-notebooks",
    is_flag=True,
    help="Keep probe notebooks running (do not stop them after probing)",
)
@click.option(
    "--pubkey",
    "probe_pubkey",
    default=None,
    help=(
        "SSH public key path for probing (defaults to ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub)"
    ),
)
@click.option(
    "--probe-timeout",
    type=int,
    default=900,
    show_default=True,
    help="Per-project probe timeout in seconds",
)
@click.option(
    "--template",
    "-t",
    "template_flag",
    is_flag=True,
    help="Create template with placeholders (skip env var detection)",
)
def init(
    global_flag: bool,
    project_flag: bool,
    force: bool,
    discover: bool,
    probe_shared_path: bool,
    probe_limit: int,
    probe_keep_notebooks: bool,
    probe_pubkey: str | None,
    probe_timeout: int,
    template_flag: bool,
) -> None:
    """Initialize Inspire CLI configuration.

    Detects environment variables and creates config.toml files.
    By default, options are auto-split by scope: global options go to
    ~/.config/inspire/config.toml, project options go to ./.inspire/config.toml.

    Use --global or --project to force all options to a single file.
    Secrets (passwords, tokens) are never written to config files for security.

    If no environment variables are detected (or with --template), creates
    a template config with placeholder values.

    Use --discover to login via the web UI, discover accessible projects and
    compute groups, and write an account-scoped catalog to the global config.

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

        \b
        # Discover projects/workspaces and write per-account catalog
        inspire init --discover
    """
    if global_flag and project_flag:
        click.echo(click.style("Error: Cannot specify both --global and --project", fg="red"))
        raise SystemExit(1)

    if discover:
        if template_flag:
            click.echo(click.style("Error: Cannot combine --discover with --template", fg="red"))
            raise SystemExit(1)
        if global_flag or project_flag:
            click.echo(
                click.style(
                    "Error: --discover always writes both global and project config",
                    fg="red",
                )
            )
            raise SystemExit(1)

        _init_discover_mode(
            force,
            probe_shared_path=probe_shared_path,
            probe_limit=probe_limit,
            probe_keep_notebooks=probe_keep_notebooks,
            probe_pubkey=probe_pubkey,
            probe_timeout=probe_timeout,
        )
        return

    if probe_shared_path:
        click.echo(click.style("Error: --probe-shared-path requires --discover", fg="red"))
        raise SystemExit(1)

    if template_flag:
        click.echo("Creating template config with placeholders.\n")
        _init_template_mode(global_flag, project_flag, force)
        return

    detected = _detect_env_vars()

    if detected:
        _init_smart_mode(detected, global_flag, project_flag, force)
    else:
        click.echo("No environment variables detected. Creating template config.\n")
        _init_template_mode(global_flag, project_flag, force)


__all__ = [
    "CONFIG_TEMPLATE",
    "_detect_env_vars",
    "_derive_shared_path_group",
    "_generate_toml_content",
    "init",
]
