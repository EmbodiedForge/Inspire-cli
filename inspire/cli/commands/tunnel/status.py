"""Tunnel status command."""

from __future__ import annotations

import sys

import click

from inspire.bridge.tunnel import get_tunnel_status
from inspire.cli.context import EXIT_GENERAL_ERROR, Context, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_error, emit_info, emit_success, emit_warning
from inspire.platform.web.browser_api.rtunnel import redact_proxy_url


@click.command("status")
@click.option("--bridge", "-b", help="Check specific bridge (shows all if not specified)")
@json_option
@pass_context
def tunnel_status(ctx: Context, bridge: str, json_output: bool = False) -> None:
    """Check tunnel configuration and SSH connectivity.

    \b
    Examples:
        inspire tunnel status          # Show all bridges
        inspire tunnel status -b mybridge
    """

    json_output = resolve_json_output(ctx, json_output)
    status = get_tunnel_status(bridge_name=bridge)
    if status.get("proxy_url"):
        status["proxy_url"] = redact_proxy_url(status["proxy_url"])

    if ctx.json_output:
        if bridge or status["bridge_name"]:
            if not status["configured"]:
                emit_error(
                    ctx,
                    error_type="NotFound",
                    message="Bridge not found",
                    exit_code=EXIT_GENERAL_ERROR,
                    data=status,
                )
                sys.exit(EXIT_GENERAL_ERROR)
            if not status["ssh_works"]:
                emit_error(
                    ctx,
                    error_type="TunnelError",
                    message=status["error"] or "SSH connection failed",
                    exit_code=EXIT_GENERAL_ERROR,
                    data=status,
                )
                sys.exit(EXIT_GENERAL_ERROR)
        click.echo(json_formatter.format_json(status))
        return

    click.echo("Inspire SSH Tunnel Status (ProxyCommand Mode)")
    click.echo("=" * 50)

    if status["bridges"]:
        click.echo(f"Bridges: {', '.join(status['bridges'])}")
        click.echo(f"Default: {status['default_bridge'] or '(none)'}")
    else:
        click.echo("Bridges: (none configured)")

    click.echo(f"rtunnel: {status['rtunnel_path'] or '(not installed)'}")
    click.echo("")

    if bridge or status["bridge_name"]:
        bridge_name = bridge or status["bridge_name"]
        click.echo(f"Bridge: {bridge_name}")
        click.echo(f"Proxy URL: {redact_proxy_url(status['proxy_url'])}")
        click.echo("")

        if status["configured"]:
            if status["ssh_works"]:
                emit_success(
                    ctx,
                    payload={"bridge": bridge_name, "status": "connected"},
                    text="SSH: Connected",
                )
            else:
                emit_warning(ctx, "SSH: Not responding")
                emit_info(ctx, "")
                emit_info(ctx, "Troubleshooting:")
                emit_info(ctx, "  1. Ensure VS Code is open on the Bridge notebook")
                emit_info(ctx, "  2. Ensure rtunnel server is running on Bridge:")
                emit_info(ctx, "     ~/.local/bin/rtunnel localhost:22222 0.0.0.0:31337")
                emit_info(ctx, "  3. Check that port 31337 is forwarded in VS Code Ports tab")
        else:
            emit_error(ctx, error_type="NotFound", message="Bridge not found", exit_code=1)
            emit_info(ctx, "")
            emit_info(ctx, "To add a bridge:")
            emit_info(ctx, "  inspire tunnel add <name> <PROXY_URL>")

        if status["error"] and status["configured"]:
            emit_error(ctx, error_type="TunnelError", message=status["error"], exit_code=1)
        if not status["configured"] or not status["ssh_works"]:
            sys.exit(EXIT_GENERAL_ERROR)
        return

    if not status["bridges"]:
        click.echo("")
        click.echo("No bridges configured. Add one with:")
        click.echo("  inspire tunnel add <name> <PROXY_URL>")
        return

    click.echo("")
    click.echo("Check specific bridge with:")
    click.echo("  inspire tunnel status -b <name>")
    click.echo("")
    if status["default_bridge"]:
        default_status = get_tunnel_status(bridge_name=status["default_bridge"])
        if default_status["ssh_works"]:
            emit_success(
                ctx,
                payload={"bridge": status["default_bridge"], "status": "connected"},
                text=f"Default bridge ({status['default_bridge']}): Connected",
            )
        else:
            emit_warning(ctx, f"Default bridge ({status['default_bridge']}): Not responding")
