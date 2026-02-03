"""Resources list command (availability)."""

from __future__ import annotations

import click

from inspire.cli.commands.resources_list_helpers import run_resources_list
from inspire.cli.context import Context, pass_context


@click.command("list")
@click.option(
    "--no-cache",
    is_flag=True,
    help="Bypass cached node availability (workspace view only)",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Thorough check: show all accessible compute groups",
)
@click.option(
    "--watch",
    "-w",
    is_flag=True,
    help="Continuously watch availability (refreshes every 30s)",
)
@click.option(
    "--interval",
    "-i",
    type=int,
    default=30,
    help="Watch refresh interval in seconds (default: 30)",
)
@click.option(
    "--workspace",
    "-ws",
    is_flag=True,
    help="Show per-node availability (workspace-scoped, browser API)",
)
@click.option(
    "--global",
    "use_global",
    is_flag=True,
    help="Deprecated: alias for --workspace (OpenAPI view removed)",
)
@pass_context
def list_resources(
    ctx: Context,
    no_cache: bool,
    show_all: bool,
    watch: bool,
    interval: int,
    workspace: bool = False,
    use_global: bool = False,
) -> None:
    """List GPU availability across compute groups.

    By default, shows accurate real-time GPU usage via browser API.
    Use --workspace for per-node availability (free/ready nodes).

    \b
    Examples:
        inspire resources list              # Accurate GPU usage (default)
        inspire resources list --workspace  # Node-level availability
        inspire resources list --all        # Include all compute groups
        inspire resources list --watch      # Watch mode
    """
    run_resources_list(
        ctx,
        no_cache=no_cache,
        show_all=show_all,
        watch=watch,
        interval=interval,
        workspace=workspace,
        use_global=use_global,
    )
