"""SSH-tunnel sync helpers for `inspire sync`."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import Context, EXIT_GENERAL_ERROR, EXIT_SUCCESS
from inspire.cli.formatters import json_formatter
from inspire.config import Config
from inspire.cli.utils.tunnel import sync_via_ssh


def sync_via_tunnel(
    ctx: Context,
    config: Config,
    *,
    branch: str,
    commit_sha: str,
    commit_msg: str,
    remote: str,
    force: bool,
    timeout: int,
    bridge_name: Optional[str] = None,
    tunnel_config=None,
) -> int:
    """Sync code via SSH tunnel (fast path)."""
    if not ctx.json_output:
        if bridge_name:
            click.echo(f"Syncing via SSH tunnel (bridge: {bridge_name})...")
        else:
            click.echo("Syncing via SSH tunnel...")

    result = sync_via_ssh(
        target_dir=config.target_dir,
        branch=branch,
        commit_sha=commit_sha,
        force=force,
        bridge_name=bridge_name,
        config=tunnel_config,
        timeout=timeout,
    )

    if result.get("success"):
        synced_sha = result.get("synced_sha") or commit_sha[:7]
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "status": "success",
                        "method": "ssh_tunnel",
                        "branch": branch,
                        "remote": remote,
                        "commit": commit_sha[:7],
                        "commit_full": commit_sha,
                        "synced_sha": synced_sha,
                        "message": commit_msg,
                        "target_dir": config.target_dir,
                    }
                )
            )
        else:
            click.echo(
                click.style("OK", fg="green")
                + f" Synced branch '{branch}' ({synced_sha[:7]}) to {config.target_dir}"
            )
            click.echo(f"  Commit: {commit_msg}")
            click.echo("  Method: SSH tunnel (fast)")
        return EXIT_SUCCESS

    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(
                "SyncError",
                str(result.get("error")),
                EXIT_GENERAL_ERROR,
            ),
            err=True,
        )
    else:
        click.echo(f"Sync failed: {result.get('error')}", err=True)
    return EXIT_GENERAL_ERROR
