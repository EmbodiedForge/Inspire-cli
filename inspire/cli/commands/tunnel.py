"""Tunnel commands for SSH access to Bridge via ProxyCommand."""

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
    get_tunnel_status,
    is_tunnel_available,
)


@click.group()
def tunnel() -> None:
    """Manage SSH tunnel for fast Bridge access.

    The SSH tunnel uses ProxyCommand mode - no background process needed.
    Commands like 'bridge exec' and 'job logs' automatically use SSH
    when a proxy URL is configured.

    \b
    Quick Start:
        1. Set up rtunnel server on Bridge (see docs/rtunnel-ssh-setup.md)
        2. inspire tunnel set-url "https://nat-notebook-inspire.../proxy/31337/"
        3. inspire tunnel status              # Verify connection
        4. inspire bridge exec "hostname"     # Now uses fast SSH!

    \b
    For direct SSH access (scp, rsync, git):
        inspire tunnel ssh-config --install
        ssh inspire-bridge
    """


@tunnel.command("status")
@pass_context
def tunnel_status(ctx: Context) -> None:
    """Check tunnel configuration and SSH connectivity.

    \b
    Example:
        inspire tunnel status
    """
    status = get_tunnel_status()

    if ctx.json_output:
        click.echo(json.dumps(status))
    else:
        click.echo("Inspire SSH Tunnel Status (ProxyCommand Mode)")
        click.echo("=" * 50)
        click.echo(f"Proxy URL: {status['proxy_url'] or '(not set)'}")
        click.echo(f"rtunnel:   {status['rtunnel_path'] or '(not installed)'}")
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
            click.echo("Status: Not configured")
            click.echo("")
            click.echo("To configure, run:")
            click.echo("  inspire tunnel set-url <PROXY_URL>")

        if status["error"] and status["configured"]:
            click.echo(f"\nError: {status['error']}")


@tunnel.command("set-url")
@click.argument("url")
@pass_context
def tunnel_set_url(ctx: Context, url: str) -> None:
    """Save the tunnel proxy URL to configuration.

    Get the URL from the Bridge notebook's VSCode Ports tab (port 31337).

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
        click.echo("")
        click.echo("Test connection with: inspire tunnel status")
        click.echo("Or for direct SSH:    inspire tunnel ssh-config --install")


@tunnel.command("ssh-config")
@click.option("--host", default="inspire-bridge", help="SSH host alias to use")
@click.option("--install", is_flag=True, help="Automatically append to ~/.ssh/config")
@pass_context
def tunnel_ssh_config(ctx: Context, host: str, install: bool) -> None:
    """Generate SSH config for direct SSH access.

    This allows using 'ssh inspire-bridge', 'scp', 'rsync', etc.
    directly without going through inspire-cli.

    \b
    Benefits:
        - Works with scp, rsync, git, and all SSH-based tools
        - Each connection gets a fresh tunnel
        - No background process to manage

    \b
    Examples:
        inspire tunnel ssh-config                    # Show config to copy
        inspire tunnel ssh-config --install          # Auto-add to ~/.ssh/config
        inspire tunnel ssh-config --host bridge      # Use custom host alias

    \b
    After setup, use:
        ssh inspire-bridge
        scp file.txt inspire-bridge:/path/
        rsync -av ./local/ inspire-bridge:/remote/
    """
    from inspire.cli.utils.tunnel import (
        generate_ssh_config,
        install_ssh_config,
        get_rtunnel_path,
    )

    try:
        config = load_tunnel_config()

        if not config.proxy_url:
            click.echo(human_formatter.format_error(
                "No proxy URL configured. Run 'inspire tunnel set-url <URL>' first."
            ), err=True)
            sys.exit(EXIT_CONFIG_ERROR)

        # Ensure rtunnel is available
        rtunnel_path = get_rtunnel_path(config)

        # Generate SSH config
        ssh_config = generate_ssh_config(
            config=config,
            host_alias=host,
            rtunnel_path=rtunnel_path,
        )

        if ctx.json_output:
            click.echo(json.dumps({
                "host": host,
                "config": ssh_config,
                "rtunnel_path": str(rtunnel_path),
                "proxy_url": config.proxy_url,
            }))
            return

        if install:
            # Auto-install to ~/.ssh/config
            result = install_ssh_config(ssh_config, host)
            if result["updated"]:
                click.echo(human_formatter.format_success(
                    f"Updated existing '{host}' entry in ~/.ssh/config"
                ))
            else:
                click.echo(human_formatter.format_success(
                    f"Added '{host}' to ~/.ssh/config"
                ))
            click.echo("")
            click.echo("You can now use:")
            click.echo(f"  ssh {host}")
            click.echo(f"  scp file.txt {host}:/path/")
            click.echo(f"  rsync -av ./local/ {host}:/remote/")
        else:
            # Just print the config
            click.echo("Add the following to your ~/.ssh/config:\n")
            click.echo("-" * 50)
            click.echo(ssh_config)
            click.echo("-" * 50)
            click.echo("")
            click.echo("Or run with --install to auto-add:")
            click.echo(f"  inspire tunnel ssh-config --install")

    except TunnelError as e:
        if ctx.json_output:
            click.echo(json_formatter.format_json_error("TunnelError", str(e), EXIT_GENERAL_ERROR))
        else:
            click.echo(human_formatter.format_error(str(e)), err=True)
        sys.exit(EXIT_GENERAL_ERROR)


@tunnel.command("test")
@pass_context
def tunnel_test(ctx: Context) -> None:
    """Test SSH connection and show timing.

    \b
    Example:
        inspire tunnel test
    """
    import time
    from inspire.cli.utils.tunnel import run_ssh_command

    config = load_tunnel_config()

    if not config.proxy_url:
        if ctx.json_output:
            click.echo(json.dumps({"error": "No proxy URL configured"}))
        else:
            click.echo(human_formatter.format_error(
                "No proxy URL configured. Run 'inspire tunnel set-url <URL>' first."
            ), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    try:
        start = time.time()
        result = run_ssh_command("hostname", config=config, timeout=30)
        elapsed = time.time() - start

        hostname = result.stdout.strip()

        if ctx.json_output:
            click.echo(json.dumps({
                "success": result.returncode == 0,
                "hostname": hostname,
                "elapsed_ms": int(elapsed * 1000),
            }))
        else:
            if result.returncode == 0:
                click.echo(human_formatter.format_success(f"Connected to: {hostname}"))
                click.echo(f"Response time: {elapsed:.2f}s")
            else:
                click.echo(human_formatter.format_error(f"Connection failed: {result.stderr}"))
                sys.exit(EXIT_GENERAL_ERROR)

    except TunnelNotAvailableError as e:
        if ctx.json_output:
            click.echo(json.dumps({"error": str(e)}))
        else:
            click.echo(human_formatter.format_error(str(e)), err=True)
        sys.exit(EXIT_GENERAL_ERROR)
    except Exception as e:
        if ctx.json_output:
            click.echo(json.dumps({"error": str(e)}))
        else:
            click.echo(human_formatter.format_error(f"Connection failed: {e}"), err=True)
        sys.exit(EXIT_GENERAL_ERROR)
