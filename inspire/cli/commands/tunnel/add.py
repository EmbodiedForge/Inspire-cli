"""Tunnel add command."""

from __future__ import annotations


import click

from ._ssh_config_sync import sync_installed_ssh_config
from inspire.bridge.tunnel import BridgeProfile, load_tunnel_config, save_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success, emit_warning
from inspire.platform.web.browser_api.rtunnel import redact_proxy_url


@click.command("add")
@click.argument("name")
@click.argument("url")
@click.option("--ssh-user", default="root", help="SSH user (default: root)")
@click.option("--ssh-port", default=22222, help="SSH port (default: 22222)")
@click.option("--set-default", is_flag=True, help="Set as default bridge")
@click.option("--no-internet", is_flag=True, help="Mark bridge as having no internet access")
@json_option
@pass_context
def tunnel_add(
    ctx: Context,
    name: str,
    url: str,
    ssh_user: str,
    ssh_port: int,
    set_default: bool,
    no_internet: bool,
    json_output: bool = False,
) -> None:
    """Add a new bridge profile.

    Get the URL from the Bridge notebook's VSCode Ports tab (port 31337).

    \b
    Examples:
        inspire tunnel add mybridge "https://nat-notebook.../proxy/31337/"
        inspire tunnel add bridge1 "https://..." --set-default
        inspire tunnel add gpu-bridge "https://..." --no-internet
    """

    json_output = resolve_json_output(ctx, json_output)
    config = load_tunnel_config()

    if not name or not name.replace("-", "").replace("_", "").isalnum():
        _handle_error(
            ctx,
            "ValidationError",
            "Invalid bridge name. Use alphanumeric, dash, underscore.",
            EXIT_CONFIG_ERROR,
        )

    profile = BridgeProfile(
        name=name,
        proxy_url=url,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        _has_internet_stored=not no_internet,
    )
    config.add_bridge(profile)

    if set_default:
        config.default_bridge = name

    save_tunnel_config(config)
    ssh_synced, ssh_sync_error = sync_installed_ssh_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "added",
                    "name": name,
                    "proxy_url": redact_proxy_url(url),
                    "is_default": name == config.default_bridge,
                    "has_internet": not no_internet,
                    "ssh_config_synced": ssh_synced,
                }
            )
        )
        return

    is_default = name == config.default_bridge
    emit_success(
        ctx,
        payload={
            "status": "added",
            "name": name,
            "proxy_url": redact_proxy_url(url),
            "is_default": is_default,
            "has_internet": not no_internet,
            "ssh_config_synced": ssh_synced,
        },
        text=f"Added bridge: {name}",
    )
    emit_info(ctx, f"  Proxy URL: {redact_proxy_url(url)}")
    emit_info(ctx, f"  SSH: {ssh_user}@localhost:{ssh_port}")
    emit_info(ctx, f"  Internet: {'has-internet' if not no_internet else 'no-internet'}")
    if is_default:
        emit_info(ctx, "  (default bridge)")
    else:
        emit_info(ctx, f"  Set as default: inspire tunnel set-default {name}")
    if ssh_synced:
        emit_info(ctx, "  SSH config: synced")
    elif ssh_sync_error:
        emit_warning(ctx, f"  SSH config sync skipped: {ssh_sync_error}")
    emit_info(ctx, "")
    emit_info(ctx, f"Test connection: inspire tunnel status -b {name}")
