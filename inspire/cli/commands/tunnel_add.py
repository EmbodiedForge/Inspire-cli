"""Tunnel add command."""

from __future__ import annotations

import sys

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.tunnel import BridgeProfile, load_tunnel_config, save_tunnel_config


@click.command("add")
@click.argument("name")
@click.argument("url")
@click.option("--ssh-user", default="root", help="SSH user (default: root)")
@click.option("--ssh-port", default=22222, help="SSH port (default: 22222)")
@click.option("--set-default", is_flag=True, help="Set as default bridge")
@click.option("--no-internet", is_flag=True, help="Mark bridge as having no internet access")
@pass_context
def tunnel_add(
    ctx: Context,
    name: str,
    url: str,
    ssh_user: str,
    ssh_port: int,
    set_default: bool,
    no_internet: bool,
) -> None:
    """Add a new bridge profile.

    Get the URL from the Bridge notebook's VSCode Ports tab (port 31337).

    \b
    Examples:
        inspire tunnel add mybridge "https://nat-notebook.../proxy/31337/"
        inspire tunnel add bridge1 "https://..." --set-default
        inspire tunnel add gpu-bridge "https://..." --no-internet
    """
    config = load_tunnel_config()

    # Validate name
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ValidationError",
                    "Invalid bridge name. Use alphanumeric, dash, underscore.",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(
                human_formatter.format_error(
                    "Invalid bridge name. Use alphanumeric, dash, underscore."
                ),
                err=True,
            )
        sys.exit(EXIT_CONFIG_ERROR)

    # Create and add profile
    profile = BridgeProfile(
        name=name,
        proxy_url=url,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        has_internet=not no_internet,
    )
    config.add_bridge(profile)

    # Set as default if requested
    if set_default:
        config.default_bridge = name

    save_tunnel_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "added",
                    "name": name,
                    "proxy_url": url,
                    "is_default": name == config.default_bridge,
                    "has_internet": not no_internet,
                }
            )
        )
        return

    is_default = name == config.default_bridge
    click.echo(f"Added bridge: {name}")
    click.echo(f"  Proxy URL: {url}")
    click.echo(f"  SSH: {ssh_user}@localhost:{ssh_port}")
    click.echo(f"  Internet: {'yes' if not no_internet else 'no'}")
    if is_default:
        click.echo(human_formatter.format_success("  (default bridge)"))
    else:
        click.echo(f"  Set as default: inspire tunnel set-default {name}")
    click.echo("")
    click.echo(f"Test connection: inspire tunnel status -b {name}")
