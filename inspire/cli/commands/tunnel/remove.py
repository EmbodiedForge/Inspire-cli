"""Tunnel remove command."""

from __future__ import annotations


import click

from ._ssh_config_sync import sync_installed_ssh_config
from inspire.bridge.tunnel import load_tunnel_config, save_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success, emit_warning


@click.command("remove")
@click.argument("name")
@json_option
@pass_context
def tunnel_remove(ctx: Context, name: str, json_output: bool = False) -> None:
    """Remove a bridge profile.

    \b
    Example:
        inspire tunnel remove mybridge
    """

    json_output = resolve_json_output(ctx, json_output)
    config = load_tunnel_config()

    if name not in config.bridges:
        _handle_error(
            ctx,
            "NotFound",
            f"Bridge '{name}' not found",
            EXIT_CONFIG_ERROR,
        )

    was_default = name == config.default_bridge
    config.remove_bridge(name)
    save_tunnel_config(config)
    ssh_synced, ssh_sync_error = sync_installed_ssh_config(config, removed_bridges={name})

    emit_success(
        ctx,
        payload={
            "status": "removed",
            "name": name,
            "new_default": config.default_bridge,
            "ssh_config_synced": ssh_synced,
        },
        text=f"Removed bridge: {name}",
    )
    if was_default and config.default_bridge:
        emit_info(ctx, f"New default: {config.default_bridge}")
    elif was_default:
        emit_info(ctx, "No default bridge set. Use: inspire tunnel set-default <name>")
    if ssh_synced:
        emit_info(ctx, "SSH config synced.")
    elif ssh_sync_error:
        emit_warning(ctx, f"SSH config sync skipped: {ssh_sync_error}")
