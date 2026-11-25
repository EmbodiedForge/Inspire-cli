"""Tunnel set-default command."""

from __future__ import annotations


import click

from ._ssh_config_sync import sync_installed_ssh_config
from inspire.bridge.tunnel import load_tunnel_config, save_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success, emit_warning


@click.command("set-default")
@click.argument("name")
@json_option
@pass_context
def tunnel_set_default(ctx: Context, name: str, json_output: bool = False) -> None:
    """Set a bridge as the default.

    \b
    Example:
        inspire tunnel set-default mybridge
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

    config.default_bridge = name
    save_tunnel_config(config)
    ssh_synced, ssh_sync_error = sync_installed_ssh_config(config)

    emit_success(
        ctx,
        payload={
            "status": "updated",
            "default": name,
            "ssh_config_synced": ssh_synced,
        },
        text=f"Default bridge set to: {name}",
    )
    if ssh_synced:
        emit_info(ctx, "SSH config synced.")
    elif ssh_sync_error:
        emit_warning(ctx, f"SSH config sync skipped: {ssh_sync_error}")
