"""Tunnel commands for SSH access to Bridge."""

import json
import sys

import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_SUCCESS,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.tunnel import (
    TunnelConfig,
    TunnelError,
    TunnelNotAvailableError,
    load_tunnel_config,
    save_tunnel_config,
    start_tunnel,
    stop_tunnel,
    get_tunnel_status,
    is_tunnel_available,
)


@click.group()
def tunnel() -> None:
    """Manage SSH tunnel for fast Bridge access.

    The SSH tunnel provides ~100x faster command execution compared to
    Gitea Actions. Once started, commands like 'bridge exec' and 'job logs'
    will automatically use the tunnel.

    \b
    Quick Start:
        1. Set up tunnel on Bridge (see docs/rtunnel-ssh-setup.md)
        2. inspire tunnel start "https://nat-notebook-inspire.../proxy/31337/"
        3. inspire bridge exec "hostname"  # Now instant!
    """


@tunnel.command("start")
@click.argument("url", required=False)
@pass_context
def tunnel_start(ctx: Context, url: str) -> None:
    """Start the SSH tunnel client.

    URL is the rtunnel proxy URL from the Bridge notebook's Ports tab.
    If not provided, uses the previously saved URL.

    \b
    Examples:
        inspire tunnel start "https://nat-notebook-inspire.../proxy/31337/"
        inspire tunnel start  # Use saved URL
    """
    try:
        pid = start_tunnel(proxy_url=url)

        config = load_tunnel_config()
        status = get_tunnel_status(config)

        if ctx.json_output:
            click.echo(json.dumps({
                "status": "started",
                "pid": pid,
                "ssh_works": status["ssh_works"],
                "proxy_url": config.proxy_url,
                "local_port": config.local_port,
            }))
        else:
            click.echo(f"Tunnel started (PID: {pid})")
            click.echo(f"  URL: {config.proxy_url}")
            click.echo(f"  Local port: {config.local_port}")

            if status["ssh_works"]:
                click.echo(human_formatter.format_success("SSH connection verified"))
            else:
                click.echo(human_formatter.format_warning(
                    "Tunnel started but SSH connection not verified yet. "
                    "Ensure Bridge sshd is running."
                ))

    except TunnelError as e:
        if ctx.json_output:
            click.echo(json_formatter.format_json_error("TunnelError", str(e), EXIT_GENERAL_ERROR))
        else:
            click.echo(human_formatter.format_error(str(e)), err=True)
        sys.exit(EXIT_GENERAL_ERROR)


@tunnel.command("stop")
@pass_context
def tunnel_stop(ctx: Context) -> None:
    """Stop the SSH tunnel client.

    \b
    Example:
        inspire tunnel stop
    """
    stopped = stop_tunnel()

    if ctx.json_output:
        click.echo(json.dumps({"status": "stopped" if stopped else "not_running"}))
    else:
        if stopped:
            click.echo("Tunnel stopped")
        else:
            click.echo("Tunnel was not running")


@tunnel.command("status")
@pass_context
def tunnel_status(ctx: Context) -> None:
    """Check tunnel status and SSH connectivity.

    \b
    Example:
        inspire tunnel status
    """
    status = get_tunnel_status()

    if ctx.json_output:
        click.echo(json.dumps(status))
    else:
        click.echo("Inspire SSH Tunnel Status")
        click.echo("=" * 40)
        click.echo(f"Proxy URL: {status['proxy_url'] or '(not set)'}")
        click.echo(f"Local port: {status['local_port']}")
        click.echo("")

        if status["running"]:
            click.echo(f"Tunnel: Running (PID: {status['pid']})")
            if status["ssh_works"]:
                click.echo(human_formatter.format_success("SSH: Connected"))
            else:
                click.echo(human_formatter.format_warning("SSH: Not responding"))
        else:
            click.echo("Tunnel: Not running")

        if status["error"]:
            click.echo(f"\nNote: {status['error']}")

        # Show rtunnel log tail when SSH fails for debugging
        if status.get("log_tail"):
            click.echo("\nRecent rtunnel log:")
            click.echo("-" * 40)
            click.echo(status["log_tail"])
            click.echo("-" * 40)
            click.echo("\nTip: Try 'inspire tunnel stop && inspire tunnel start' to reconnect")


@tunnel.command("set-url")
@click.argument("url")
@pass_context
def tunnel_set_url(ctx: Context, url: str) -> None:
    """Save the tunnel proxy URL to configuration.

    This saves the URL for future 'tunnel start' commands.
    Get the URL from the Bridge notebook's VSCode Ports tab.

    \b
    Example:
        inspire tunnel set-url "https://nat-notebook-inspire.../proxy/31337/"
    """
    config = load_tunnel_config()
    config.proxy_url = url
    save_tunnel_config(config)

    if ctx.json_output:
        click.echo(json.dumps({"status": "saved", "proxy_url": url}))
    else:
        click.echo(f"Proxy URL saved: {url}")
        click.echo("\nStart tunnel with: inspire tunnel start")
