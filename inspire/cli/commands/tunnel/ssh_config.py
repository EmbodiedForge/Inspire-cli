"""Tunnel ssh-config command."""

from __future__ import annotations


import click

from inspire.bridge.tunnel import (
    TunnelError,
    generate_all_ssh_configs,
    generate_ssh_config,
    get_rtunnel_path,
    install_all_ssh_configs,
    install_ssh_config,
    load_tunnel_config,
)
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success


@click.command("ssh-config")
@click.option("--bridge", "-b", help="Generate config for specific bridge only")
@click.option("--install", is_flag=True, help="Automatically append to ~/.ssh/config")
@json_option
@pass_context
def tunnel_ssh_config(ctx: Context, bridge: str, install: bool, json_output: bool = False) -> None:
    """Generate SSH config for direct SSH access to all bridges.

    This allows using 'ssh <bridge-name>', 'scp', 'rsync', etc.
    directly without going through inspire-cli.

    \b
    Benefits:
        - Works with scp, git, and every other SSH-based tool
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

    \b
    rsync caveat: these entries only cover the local side. Plain rsync also needs an
    rsync binary inside the container, and the pods do not ship one, so the example above
    fails with "remote command rsync not found" until you install it. 'inspire bridge sync'
    installs it on demand and drives rsync over this same ProxyCommand, so prefer it for
    whole-directory pushes.
    """

    json_output = resolve_json_output(ctx, json_output)
    try:
        config = load_tunnel_config()

        if not config.bridges:
            _handle_error(
                ctx,
                "ConfigError",
                "No bridges configured. Run 'inspire tunnel add <name> <URL>' first.",
                EXIT_CONFIG_ERROR,
            )

        rtunnel_path = get_rtunnel_path(config)

        if bridge:
            bridge_profile = config.get_bridge(bridge)
            if not bridge_profile:
                _handle_error(
                    ctx,
                    "NotFound",
                    f"Bridge '{bridge}' not found",
                    EXIT_CONFIG_ERROR,
                )

            ssh_config = generate_ssh_config(bridge_profile, rtunnel_path, host_alias=bridge)
            # --install has to happen before the JSON branch returns, otherwise
            # `--json --install` reported success having installed nothing.
            install_result = install_ssh_config(ssh_config, bridge) if install else None

            if ctx.json_output:
                payload: dict = {
                    "bridge": bridge,
                    "config": ssh_config,
                    "rtunnel_path": str(rtunnel_path),
                }
                if install_result is not None:
                    payload["install"] = install_result
                click.echo(json_formatter.format_json(payload))
                return

            if install_result is not None:
                if install_result["updated"]:
                    emit_success(
                        ctx,
                        payload={
                            "bridge": bridge,
                            "action": "updated",
                            "config_path": "~/.ssh/config",
                        },
                        text=f"Updated '{bridge}' entry in ~/.ssh/config",
                    )
                else:
                    emit_success(
                        ctx,
                        payload={
                            "bridge": bridge,
                            "action": "added",
                            "config_path": "~/.ssh/config",
                        },
                        text=f"Added '{bridge}' to ~/.ssh/config",
                    )
                emit_info(ctx, "")
                emit_info(ctx, "You can now use:")
                emit_info(ctx, f"  ssh {bridge}")
            else:
                # stdout is the artifact: `inspire tunnel ssh-config > ~/.ssh/config` and
                # `ssh -F <(inspire tunnel ssh-config)` must both work, so prose goes to stderr and the
                # separator is a `#` comment, which ssh_config actually allows. See issue #32.
                click.echo(f"SSH config for bridge '{bridge}':", err=True)
                click.echo(f"# bridge: {bridge}")
                click.echo(ssh_config)
            return

        all_configs = generate_all_ssh_configs(config)
        install_result = install_all_ssh_configs(config) if install else None

        if ctx.json_output:
            payload = {
                "bridges": list(config.bridges.keys()),
                "config": all_configs,
                "rtunnel_path": str(rtunnel_path),
            }
            if install_result is not None:
                payload["install"] = install_result
            click.echo(json_formatter.format_json(payload))
            return

        if install_result is not None:
            emit_success(
                ctx,
                payload={
                    "bridges": list(config.bridges.keys()),
                    "action": "added",
                    "config_path": "~/.ssh/config",
                },
                text=f"Added {len(config.bridges)} bridge(s) to ~/.ssh/config",
            )
            emit_info(ctx, "")
            emit_info(ctx, "You can now use:")
            for b in sorted(config.bridges.keys()):
                emit_info(ctx, f"  ssh {b}")
        else:
            click.echo("SSH config for all bridges:", err=True)
            click.echo("# generated by inspire tunnel ssh-config")
            click.echo(all_configs)
            emit_info(ctx, "")
            emit_info(ctx, "Or run with --install to auto-add:")
            emit_info(ctx, "  inspire tunnel ssh-config --install")

    except TunnelError as e:
        _handle_error(
            ctx,
            "TunnelError",
            str(e),
            EXIT_GENERAL_ERROR,
        )
