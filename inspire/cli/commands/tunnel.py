"""Tunnel commands for SSH access to Bridge via ProxyCommand."""

import sys
from pathlib import Path

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
    BridgeNotFoundError,
    BridgeProfile,
    load_tunnel_config,
    save_tunnel_config,
    get_tunnel_status,
    is_tunnel_available,
)


@click.group()
def tunnel() -> None:
    """Manage SSH tunnels for fast Bridge access.

    Supports multiple bridge profiles. Commands like 'bridge exec' and
    'job logs' automatically use SSH when a bridge is configured.

    \b
    Quick Start:
        1. Set up rtunnel server on Bridge
        2. inspire tunnel add mybridge "https://nat-notebook.../proxy/31337/"
        3. inspire tunnel status              # Verify connection
        4. inspire bridge exec "hostname"     # Now uses fast SSH!

    \b
    Multiple bridges:
        inspire tunnel add bridge1 "https://..."
        inspire tunnel add bridge2 "https://..."
        inspire tunnel list
        inspire bridge exec --bridge bridge2 "hostname"

    \b
    For direct SSH access (scp, rsync, git):
        inspire tunnel ssh-config --install
        ssh bridge1
    """


@tunnel.command("status")
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
    config = load_tunnel_config()
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
    else:
        # No specific bridge selected - show summary
        if not status["bridges"]:
            click.echo("")
            click.echo("No bridges configured. Add one with:")
            click.echo("  inspire tunnel add <name> <PROXY_URL>")
        else:
            click.echo("")
            click.echo("Check specific bridge with:")
            click.echo("  inspire tunnel status -b <name>")
            click.echo("")
            # Quick test of default bridge
            if status["default_bridge"]:
                default_status = get_tunnel_status(bridge_name=status["default_bridge"])
                if default_status["ssh_works"]:
                    click.echo(f"Default bridge ({status['default_bridge']}): " +
                             human_formatter.format_success("Connected"))
                else:
                    click.echo(f"Default bridge ({status['default_bridge']}): " +
                             human_formatter.format_warning("Not responding"))


@tunnel.command("add")
@click.argument("name")
@click.argument("url")
@click.option("--ssh-user", default="root", help="SSH user (default: root)")
@click.option("--ssh-port", default=22222, help="SSH port (default: 22222)")
@click.option("--set-default", is_flag=True, help="Set as default bridge")
@pass_context
def tunnel_add(
    ctx: Context,
    name: str,
    url: str,
    ssh_user: str,
    ssh_port: int,
    set_default: bool,
) -> None:
    """Add a new bridge profile.

    Get the URL from the Bridge notebook's VSCode Ports tab (port 31337).

    \b
    Examples:
        inspire tunnel add mybridge "https://nat-notebook.../proxy/31337/"
        inspire tunnel add bridge1 "https://..." --set-default
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
            click.echo(human_formatter.format_error(
                "Invalid bridge name. Use alphanumeric, dash, underscore."
            ), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    # Create and add profile
    profile = BridgeProfile(
        name=name,
        proxy_url=url,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
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
                }
            )
        )
    else:
        is_default = name == config.default_bridge
        click.echo(f"Added bridge: {name}")
        click.echo(f"  Proxy URL: {url}")
        click.echo(f"  SSH: {ssh_user}@localhost:{ssh_port}")
        if is_default:
            click.echo(human_formatter.format_success("  (default bridge)"))
        else:
            click.echo(f"  Set as default: inspire tunnel set-default {name}")
        click.echo("")
        click.echo("Test connection: inspire tunnel status -b {}".format(name))


@tunnel.command("remove")
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

    was_default = (name == config.default_bridge)
    config.remove_bridge(name)
    save_tunnel_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "removed",
                    "name": name,
                    "new_default": config.default_bridge,
                }
            )
        )
    else:
        click.echo(f"Removed bridge: {name}")
        if was_default and config.default_bridge:
            click.echo(f"New default: {config.default_bridge}")
        elif was_default:
            click.echo("No default bridge set. Use: inspire tunnel set-default <name>")


@tunnel.command("set-default")
@click.argument("name")
@pass_context
def tunnel_set_default(ctx: Context, name: str) -> None:
    """Set a bridge as the default.

    \b
    Example:
        inspire tunnel set-default mybridge
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

    config.default_bridge = name
    save_tunnel_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "updated",
                    "default": name,
                }
            )
        )
    else:
        click.echo(human_formatter.format_success(f"Default bridge set to: {name}"))


@tunnel.command("list")
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
        is_default = (bridge.name == config.default_bridge)
        default_mark = "* " if is_default else "  "
        click.echo(f"{default_mark}{bridge.name}:")
        click.echo(f"    URL: {bridge.proxy_url}")
        click.echo(f"    SSH: {bridge.ssh_user}@localhost:{bridge.ssh_port}")
        if is_default:
            click.echo(human_formatter.format_success("    (default)"))
    click.echo("")
    click.echo("* = default bridge")


@tunnel.command("ssh-config")
@click.option("--bridge", "-b", help="Generate config for specific bridge only")
@click.option("--install", is_flag=True, help="Automatically append to ~/.ssh/config")
@pass_context
def tunnel_ssh_config(ctx: Context, bridge: str, install: bool) -> None:
    """Generate SSH config for direct SSH access to all bridges.

    This allows using 'ssh <bridge-name>', 'scp', 'rsync', etc.
    directly without going through inspire-cli.

    \b
    Benefits:
        - Works with scp, rsync, git, and all SSH-based tools
        - Each connection gets a fresh tunnel
        - No background process to manage

    \b
    Examples:
        inspire tunnel ssh-config                    # Show all bridges config
        inspire tunnel ssh-config --install          # Auto-add to ~/.ssh/config
        inspire tunnel ssh-config -b mybridge       # Show specific bridge only

    \b
    After setup, use:
        ssh <bridge-name>
        scp file.txt <bridge-name>:/path/
        rsync -av ./local/ <bridge-name>:/remote/
    """
    from inspire.cli.utils.tunnel import (
        generate_ssh_config,
        generate_all_ssh_configs,
        install_ssh_config,
        get_rtunnel_path,
    )
    import re

    try:
        config = load_tunnel_config()

        if not config.bridges:
            click.echo(human_formatter.format_error(
                "No bridges configured. Run 'inspire tunnel add <name> <URL>' first."
            ), err=True)
            sys.exit(EXIT_CONFIG_ERROR)

        # Ensure rtunnel is available
        rtunnel_path = get_rtunnel_path(config)

        if bridge:
            # Single bridge config
            bridge_profile = config.get_bridge(bridge)
            if not bridge_profile:
                click.echo(human_formatter.format_error(f"Bridge '{bridge}' not found"), err=True)
                sys.exit(EXIT_CONFIG_ERROR)

            ssh_config = generate_ssh_config(bridge_profile, rtunnel_path, host_alias=bridge)

            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "bridge": bridge,
                            "config": ssh_config,
                            "rtunnel_path": str(rtunnel_path),
                        }
                    )
                )
                return

            if install:
                result = install_ssh_config(ssh_config, bridge)
                if result["updated"]:
                    click.echo(human_formatter.format_success(
                        f"Updated '{bridge}' entry in ~/.ssh/config"
                    ))
                else:
                    click.echo(human_formatter.format_success(
                        f"Added '{bridge}' to ~/.ssh/config"
                    ))
                click.echo("")
                click.echo("You can now use:")
                click.echo(f"  ssh {bridge}")
            else:
                click.echo(f"SSH config for bridge '{bridge}':\n")
                click.echo("-" * 50)
                click.echo(ssh_config)
                click.echo("-" * 50)
        else:
            # All bridges config
            all_configs = generate_all_ssh_configs(config)

            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "bridges": list(config.bridges.keys()),
                            "config": all_configs,
                            "rtunnel_path": str(rtunnel_path),
                        }
                    )
                )
                return

            if install:
                # Install all bridges to ~/.ssh/config
                ssh_config_path = Path.home() / ".ssh" / "config"
                ssh_config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

                # Remove old inspire-bridge entries if exist
                if ssh_config_path.exists():
                    content = ssh_config_path.read_text()
                    # Remove all blocks that contain inspire bridge names
                    for bridge_name in list(config.bridges.keys()):
                        # Match and remove the host block
                        pattern = rf'Host\s+.*?\b{re.escape(bridge_name)}\b.*?(?=\nHost\s|\Z)'
                        content = re.sub(pattern, '', content, flags=re.DOTALL | re.MULTILINE)
                    ssh_config_path.write_text(content)

                # Append new configs
                with open(ssh_config_path, "a") as f:
                    f.write("\n")
                    f.write("# Inspire Bridges (auto-generated)\n")
                    f.write(all_configs)
                    f.write("\n")

                click.echo(human_formatter.format_success(
                    f"Added {len(config.bridges)} bridge(s) to ~/.ssh/config"
                ))
                click.echo("")
                click.echo("You can now use:")
                for b in sorted(config.bridges.keys()):
                    click.echo(f"  ssh {b}")
            else:
                click.echo("SSH config for all bridges:\n")
                click.echo("-" * 50)
                click.echo(all_configs)
                click.echo("-" * 50)
                click.echo("")
                click.echo("Or run with --install to auto-add:")
                click.echo("  inspire tunnel ssh-config --install")

    except TunnelError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("TunnelError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(str(e)), err=True)
        sys.exit(EXIT_GENERAL_ERROR)


@tunnel.command("test")
@click.option("--bridge", "-b", help="Bridge to test (uses default if not specified)")
@pass_context
def tunnel_test(ctx: Context, bridge: str) -> None:
    """Test SSH connection and show timing.

    \b
    Examples:
        inspire tunnel test
        inspire tunnel test -b mybridge
    """
    import time
    from inspire.cli.utils.tunnel import run_ssh_command

    config = load_tunnel_config()
    bridge_profile = config.get_bridge(bridge)

    if not bridge_profile:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    "No bridge configured",
                    EXIT_CONFIG_ERROR,
                    hint="Run 'inspire tunnel add <name> <URL>' first.",
                ),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(
                "No bridge configured. Run 'inspire tunnel add <name> <URL>' first."
            ), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    try:
        start = time.time()
        result = run_ssh_command("hostname", bridge_name=bridge_profile.name, config=config, timeout=30)
        elapsed = time.time() - start

        hostname = result.stdout.strip()

        if ctx.json_output:
            if result.returncode == 0:
                click.echo(
                    json_formatter.format_json(
                        {
                            "bridge": bridge_profile.name,
                            "hostname": hostname,
                            "elapsed_ms": int(elapsed * 1000),
                        }
                    )
                )
            else:
                click.echo(
                    json_formatter.format_json_error(
                        "TunnelError",
                        f"Connection failed: {result.stderr}",
                        EXIT_GENERAL_ERROR,
                    ),
                    err=True,
                )
                sys.exit(EXIT_GENERAL_ERROR)
        else:
            if result.returncode == 0:
                click.echo(human_formatter.format_success(f"Bridge '{bridge_profile.name}': Connected to {hostname}"))
                click.echo(f"Response time: {elapsed:.2f}s")
            else:
                click.echo(human_formatter.format_error(f"Connection failed: {result.stderr}"))
                sys.exit(EXIT_GENERAL_ERROR)

    except TunnelNotAvailableError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("TunnelError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(str(e)), err=True)
        sys.exit(EXIT_GENERAL_ERROR)
    except Exception as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("Error", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(f"Connection failed: {e}"), err=True)
        sys.exit(EXIT_GENERAL_ERROR)
