"""Tunnel status command."""

from __future__ import annotations

import click

from inspire.cli.context import Context, pass_context
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.tunnel import get_tunnel_status


@click.command("status")
@click.option("--bridge", "-b", help="Check specific bridge (shows all if not specified)")
@pass_context
def tunnel_status(ctx: Context, bridge: str) -> None:
    """Check tunnel configuration and SSH connectivity.

    \b
    Examples:
        inspire tunnel status          # Show all bridges
        inspire tunnel status -b mybridge
    """
    status = get_tunnel_status(bridge_name=bridge)

    if ctx.json_output:
        click.echo(json_formatter.format_json(status))
        return

    click.echo("Inspire SSH Tunnel Status (ProxyCommand Mode)")
    click.echo("=" * 50)

    # Show all bridges
    if status["bridges"]:
        click.echo(f"Bridges: {', '.join(status['bridges'])}")
        click.echo(f"Default: {status['default_bridge'] or '(none)'}")
    else:
        click.echo("Bridges: (none configured)")

    click.echo(f"rtunnel: {status['rtunnel_path'] or '(not installed)'}")
    click.echo("")

    if bridge or status["bridge_name"]:
        # Single bridge status
        bridge_name = bridge or status["bridge_name"]
        click.echo(f"Bridge: {bridge_name}")
        click.echo(f"Proxy URL: {status['proxy_url']}")
        click.echo("")

        if status["configured"]:
            if status["ssh_works"]:
                click.echo(human_formatter.format_success("SSH: Connected"))
            else:
                click.echo(human_formatter.format_warning("SSH: Not responding"))
                click.echo("")
                click.echo("Troubleshooting:")
                click.echo("  1. Ensure VS Code is open on the Bridge notebook")
                click.echo("  2. Ensure rtunnel server is running on Bridge:")
                click.echo("     ~/.local/bin/rtunnel localhost:22222 0.0.0.0:31337")
                click.echo("  3. Check that port 31337 is forwarded in VS Code Ports tab")
        else:
            click.echo("Status: Not found")
            click.echo("")
            click.echo("To add a bridge:")
            click.echo("  inspire tunnel add <name> <PROXY_URL>")

        if status["error"] and status["configured"]:
            click.echo(f"\nError: {status['error']}")
        return

    # No specific bridge selected - show summary
    if not status["bridges"]:
        click.echo("")
        click.echo("No bridges configured. Add one with:")
        click.echo("  inspire tunnel add <name> <PROXY_URL>")
        return

    click.echo("")
    click.echo("Check specific bridge with:")
    click.echo("  inspire tunnel status -b <name>")
    click.echo("")
    # Quick test of default bridge
    if status["default_bridge"]:
        default_status = get_tunnel_status(bridge_name=status["default_bridge"])
        if default_status["ssh_works"]:
            click.echo(
                f"Default bridge ({status['default_bridge']}): "
                + human_formatter.format_success("Connected")
            )
        else:
            click.echo(
                f"Default bridge ({status['default_bridge']}): "
                + human_formatter.format_warning("Not responding")
            )
