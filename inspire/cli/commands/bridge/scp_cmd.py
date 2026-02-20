"""Bridge scp command -- transfer files to/from Bridge via SCP."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.bridge.tunnel import (
    TunnelNotAvailableError,
    BridgeNotFoundError,
    is_tunnel_available,
    load_tunnel_config,
)
from inspire.bridge.tunnel.scp import run_scp_transfer
from inspire.cli.formatters import json_formatter


@click.command("scp")
@click.argument("source")
@click.argument("destination")
@click.option("--download", "-d", is_flag=True, help="Download from remote (default is upload)")
@click.option("--recursive", "-r", is_flag=True, help="Copy directories recursively")
@click.option("--bridge", "-b", help="Bridge profile to use")
@click.option("--timeout", "-t", type=int, default=None, help="Timeout in seconds")
@pass_context
def bridge_scp(
    ctx: Context,
    source: str,
    destination: str,
    download: bool,
    recursive: bool,
    bridge: Optional[str],
    timeout: Optional[int],
) -> None:
    """Transfer files to/from Bridge via SCP.

    By default, uploads SOURCE (local) to DESTINATION (remote).
    Use --download to download SOURCE (remote) to DESTINATION (local).

    \b
    Examples:
        inspire bridge scp ./model.py /tmp/model.py
        inspire bridge scp ./data/ /tmp/data/ -r
        inspire bridge scp -d /tmp/results.tar.gz ./results.tar.gz
        inspire bridge scp -d /tmp/checkpoints/ ./checkpoints/ -r
        inspire bridge scp ./bundle.tar /tmp/ --bridge gpu-main
    """
    # Validate local path exists for uploads
    if not download:
        local = Path(source)
        if not local.exists():
            msg = f"Local path not found: {source}"
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error("FileNotFound", msg, EXIT_GENERAL_ERROR),
                    err=True,
                )
            else:
                click.echo(f"Error: {msg}", err=True)
            sys.exit(EXIT_GENERAL_ERROR)

        # Auto-enable recursive for directories
        if local.is_dir() and not recursive:
            recursive = True

    tunnel_config = load_tunnel_config()
    if bridge and tunnel_config.get_bridge(bridge) is None:
        message = f"Bridge '{bridge}' not found."
        hint = "Run 'inspire tunnel list' to see available bridge profiles."
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "BridgeNotFound",
                    message,
                    EXIT_GENERAL_ERROR,
                    hint=hint,
                ),
                err=True,
            )
        else:
            click.echo(f"Error: {message}", err=True)
            click.echo(f"Hint: {hint}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    if not is_tunnel_available(bridge_name=bridge, config=tunnel_config):
        hint = (
            "Run 'inspire tunnel status' to troubleshoot. "
            "If needed, re-create the bridge via "
            "'inspire notebook ssh <notebook-id> --save-as <name>'."
        )
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "TunnelError",
                    "SSH tunnel not available",
                    EXIT_GENERAL_ERROR,
                    hint=hint,
                ),
                err=True,
            )
        else:
            click.echo("Error: SSH tunnel not available", err=True)
            click.echo(f"Hint: {hint}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    if download:
        local_path, remote_path = destination, source
    else:
        local_path, remote_path = source, destination

    direction = "download" if download else "upload"

    if not ctx.json_output and ctx.debug:
        click.echo(f"SCP {direction}: {source} -> {destination}")
        if bridge:
            click.echo(f"Bridge: {bridge}")
        if recursive:
            click.echo("Mode: recursive")

    try:
        result = run_scp_transfer(
            local_path=local_path,
            remote_path=remote_path,
            download=download,
            recursive=recursive,
            bridge_name=bridge,
            config=tunnel_config,
            timeout=timeout,
        )

        if result.returncode != 0:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "SCPFailed",
                        f"SCP {direction} failed with exit code {result.returncode}",
                        EXIT_GENERAL_ERROR,
                    ),
                    err=True,
                )
            else:
                click.echo(
                    f"Error: SCP {direction} failed with exit code {result.returncode}",
                    err=True,
                )
            sys.exit(EXIT_GENERAL_ERROR)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "status": "success",
                        "direction": direction,
                        "source": source,
                        "destination": destination,
                        "recursive": recursive,
                    }
                )
            )
        else:
            click.echo("OK")

    except BridgeNotFoundError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("BridgeNotFound", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)
    except TunnelNotAvailableError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("TunnelError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)
    except subprocess.TimeoutExpired:
        msg = f"SCP {direction} timed out after {timeout}s"
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("Timeout", msg, EXIT_TIMEOUT),
                err=True,
            )
        else:
            click.echo(f"Error: {msg}", err=True)
        sys.exit(EXIT_TIMEOUT)
