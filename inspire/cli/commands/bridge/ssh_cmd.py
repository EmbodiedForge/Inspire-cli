"""Bridge ssh command -- open an interactive SSH shell to Bridge."""

from __future__ import annotations

import os
import sys
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.config import Config, ConfigError, build_env_exports
from inspire.bridge.tunnel import (
    is_tunnel_available,
    get_ssh_command_args,
    load_tunnel_config,
)
from inspire.cli.formatters import json_formatter


@click.command("ssh")
@click.option("--bridge", "-b", help="Bridge profile to connect")
@pass_context
def bridge_ssh(ctx: Context, bridge: Optional[str]) -> None:
    """Open an interactive SSH shell to Bridge.

    Requires an active SSH tunnel. Start with: inspire tunnel start

    \b
    Example:
        inspire tunnel start
        inspire bridge ssh
    """
    try:
        config, _ = Config.from_files_and_env(require_target_dir=True, require_credentials=False)
    except ConfigError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("ConfigError", str(e), EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(f"Configuration error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    tunnel_config = load_tunnel_config()

    if not is_tunnel_available(bridge_name=bridge, config=tunnel_config):
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "TunnelError",
                    "SSH tunnel not available",
                    EXIT_GENERAL_ERROR,
                    hint="Run 'inspire tunnel start' first",
                ),
                err=True,
            )
        else:
            click.echo("Error: SSH tunnel not available", err=True)
            click.echo("Hint: Run 'inspire tunnel start' first", err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    # Build interactive SSH command with env exports and cd to target dir
    env_exports = build_env_exports(config.remote_env)
    ssh_args = get_ssh_command_args(
        bridge_name=bridge,
        config=tunnel_config,
        remote_command=f'{env_exports}cd "{config.target_dir}" && exec $SHELL -l',
    )

    if not ctx.json_output:
        click.echo("Opening SSH connection to Bridge...")
        if bridge:
            click.echo(f"Bridge: {bridge}")
        click.echo(f"Working directory: {config.target_dir}")
        click.echo("Press Ctrl+D or type 'exit' to disconnect")
        click.echo("")

    # Replace current process with SSH
    os.execvp(ssh_args[0], ssh_args)
