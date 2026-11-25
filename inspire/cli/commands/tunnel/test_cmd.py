"""Tunnel test command."""

from __future__ import annotations


import click

from inspire.bridge.tunnel import TunnelNotAvailableError, load_tunnel_config, run_ssh_command
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success


@click.command("test")
@click.option("--bridge", "-b", help="Bridge to test (uses default if not specified)")
@json_option
@pass_context
def tunnel_test(ctx: Context, bridge: str, json_output: bool = False) -> None:
    """Test SSH connection and show timing.

    \b
    Examples:
        inspire tunnel test
        inspire tunnel test -b mybridge
    """

    json_output = resolve_json_output(ctx, json_output)
    import time

    config = load_tunnel_config()
    bridge_profile = config.get_bridge(bridge)

    if not bridge_profile:
        _handle_error(
            ctx,
            "ConfigError",
            "No bridge configured",
            EXIT_CONFIG_ERROR,
            hint="Run 'inspire tunnel add <name> <URL>' first.",
        )

    try:
        start = time.time()
        result = run_ssh_command(
            "hostname", bridge_name=bridge_profile.name, config=config, timeout=30
        )
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
                _handle_error(
                    ctx,
                    "TunnelError",
                    f"Connection failed: {result.stderr}",
                    EXIT_GENERAL_ERROR,
                )
        else:
            if result.returncode == 0:
                emit_success(
                    ctx,
                    payload={
                        "bridge": bridge_profile.name,
                        "hostname": hostname,
                        "elapsed_ms": int(elapsed * 1000),
                    },
                    text=f"Bridge '{bridge_profile.name}': Connected to {hostname}",
                )
                emit_info(ctx, f"Response time: {elapsed:.2f}s")
            else:
                _handle_error(
                    ctx,
                    "TunnelError",
                    f"Connection failed: {result.stderr}",
                    EXIT_GENERAL_ERROR,
                )

    except TunnelNotAvailableError as e:
        _handle_error(
            ctx,
            "TunnelError",
            str(e),
            EXIT_GENERAL_ERROR,
        )
    except Exception as e:
        _handle_error(
            ctx,
            "Error",
            f"Connection failed: {e}",
            EXIT_GENERAL_ERROR,
        )
