"""Bridge commands for executing raw commands on the Bridge runner."""

from __future__ import annotations

import click

from inspire.cli.commands.tunnel.list_cmd import tunnel_list

from .benchmark_cmd import benchmark_command
from .exec_cmd import exec_command
from .scp_cmd import bridge_scp
from .ssh_cmd import bridge_ssh
from .sync_cmd import bridge_sync


@click.group()
def bridge() -> None:
    """Run commands on the Bridge runner (executes in INSPIRE_TARGET_DIR)."""


@click.command("list")
@click.option(
    "--no-check",
    is_flag=True,
    help="Skip live SSH connectivity check (faster output).",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show full details (URL, SSH user, internet status).",
)
@click.option(
    "--columns",
    "-c",
    default="name,status,notebook,internet",
    help="Comma-separated columns to display.",
)
@click.option(
    "--limit",
    "-n",
    type=int,
    default=10,
    help="Maximum number of bridges to display.",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Show all bridges (ignore --limit).",
)
@click.pass_context
def bridge_list(ctx, no_check, verbose, columns, limit, show_all):
    """List all configured bridges (alias for 'inspire tunnel list').

    Shows bridge status, connected notebook, and SSH connectivity.

    \b
    Example:
        inspire bridge list
        inspire bridge list --verbose
        inspire bridge list --no-check
    """
    # Forward to tunnel_list: create a fresh subcontext with correct params
    sub_ctx = tunnel_list.make_context(tunnel_list.name or "list", [], parent=ctx)
    sub_ctx.params.update(
        no_check=no_check,
        verbose=verbose,
        columns=columns,
        limit=limit,
        show_all=show_all,
        json_output=False,
    )
    # Call tunnel_list.Command.invoke directly (bypasses __call__'s main() which expects CLI args)
    return tunnel_list.invoke(sub_ctx)


bridge.add_command(exec_command)
bridge.add_command(benchmark_command)
bridge.add_command(bridge_scp)
bridge.add_command(bridge_sync)
bridge.add_command(bridge_ssh)
bridge.add_command(bridge_list, name="list")

__all__ = ["bridge"]
