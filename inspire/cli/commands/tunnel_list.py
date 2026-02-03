"""Tunnel list command."""

from __future__ import annotations

import click

from inspire.cli.context import Context, pass_context
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.tunnel import load_tunnel_config


@click.command("list")
@pass_context
def tunnel_list(ctx: Context) -> None:
    """List all configured bridges.

    \b
    Example:
        inspire tunnel list
    """
    config = load_tunnel_config()

    bridges = config.list_bridges()

    if not bridges:
        if ctx.json_output:
            click.echo(json_formatter.format_json({"bridges": [], "default": None}))
        else:
            click.echo("No bridges configured.")
            click.echo("")
            click.echo("Add one with: inspire tunnel add <name> <URL>")
        return

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "bridges": [b.to_dict() for b in bridges],
                    "default": config.default_bridge,
                }
            )
        )
        return

    # Human-readable output
    click.echo("Configured bridges:")
    click.echo("=" * 50)
    for bridge in sorted(bridges, key=lambda b: b.name):
        is_default = bridge.name == config.default_bridge
        default_mark = "* " if is_default else "  "
        no_internet_mark = " [no internet]" if not bridge.has_internet else ""
        click.echo(f"{default_mark}{bridge.name}:{no_internet_mark}")
        click.echo(f"    URL: {bridge.proxy_url}")
        click.echo(f"    SSH: {bridge.ssh_user}@localhost:{bridge.ssh_port}")
        click.echo(f"    Internet: {'yes' if bridge.has_internet else 'no'}")
        if is_default:
            click.echo(human_formatter.format_success("    (default)"))
    click.echo("")
    click.echo("* = default bridge")
