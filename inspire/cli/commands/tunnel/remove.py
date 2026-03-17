"""Tunnel remove command."""

from __future__ import annotations

import sys

import click

from inspire.bridge.tunnel import load_tunnel_config, save_tunnel_config
from inspire.cli.commands.tunnel._ssh_config_sync import sync_installed_ssh_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter


@click.command("remove")
@click.argument("name")
@pass_context
def tunnel_remove(ctx: Context, name: str) -> None:
    """Remove a bridge profile.

    \b
    Example:
        inspire tunnel remove mybridge
    """
    config = load_tunnel_config()

    if name not in config.bridges:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "NotFound",
                    f"Bridge '{name}' not found",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(f"Bridge '{name}' not found"), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    was_default = name == config.default_bridge
    config.remove_bridge(name)
    save_tunnel_config(config)
    ssh_synced, ssh_sync_error = sync_installed_ssh_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "removed",
                    "name": name,
                    "new_default": config.default_bridge,
                    "ssh_config_synced": ssh_synced,
                }
            )
        )
        return

    click.echo(f"Removed bridge: {name}")
    if was_default and config.default_bridge:
        click.echo(f"New default: {config.default_bridge}")
    elif was_default:
        click.echo("No default bridge set. Use: inspire tunnel set-default <name>")
    if ssh_synced:
        click.echo("SSH config synced.")
    elif ssh_sync_error:
        click.echo(human_formatter.format_warning(f"SSH config sync skipped: {ssh_sync_error}"))
