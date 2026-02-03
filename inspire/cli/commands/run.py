"""Run command - Quick job submission with smart resource allocation.

Usage:
    inspire run "python train.py"
    inspire run "bash train.sh" --gpus 4 --type H100
    inspire run "python train.py" --sync --watch
"""

import os

import click

from inspire.cli.commands.run_flow import run_flow
from inspire.cli.context import Context, pass_context


@click.command()
@click.argument("command")
@click.option(
    "--gpus",
    "-g",
    type=int,
    default=8,
    help="Number of GPUs (default: 8)",
)
@click.option(
    "--type",
    "gpu_type",
    type=click.Choice(["H100", "H200"], case_sensitive=False),
    default="H200",
    help="GPU type (default: H200)",
)
@click.option("--name", "-n", help="Job name (auto-generated if not specified)")
@click.option(
    "--sync",
    "-s",
    is_flag=True,
    help="Sync code before running",
)
@click.option(
    "--watch",
    "-w",
    is_flag=True,
    help="Sync, run, then follow logs",
)
@click.option(
    "--priority",
    type=int,
    default=lambda: int(os.environ.get("INSP_PRIORITY", "6")),
    help="Task priority 1-10 (default: 6, env: INSP_PRIORITY)",
)
@click.option(
    "--location",
    help="Preferred datacenter location (overrides auto-selection)",
)
@click.option("--workspace", help="Workspace name (from [workspaces])")
@click.option(
    "--workspace-id",
    "workspace_id_override",
    help="Workspace ID override (highest precedence)",
)
@click.option(
    "--max-time",
    type=float,
    default=100.0,
    help="Max runtime in hours (default: 100)",
)
@click.option(
    "--image",
    default=lambda: os.environ.get("INSP_IMAGE"),
    help="Custom Docker image",
)
@click.option(
    "--nodes",
    type=int,
    default=1,
    help="Number of nodes for multi-node training (default: 1)",
)
@pass_context
def run(
    ctx: Context,
    command: str,
    gpus: int,
    gpu_type: str,
    name: str | None,
    sync: bool,
    watch: bool,
    priority: int,
    location: str | None,
    workspace: str | None,
    workspace_id_override: str | None,
    max_time: float,
    image: str | None,
    nodes: int,
):
    """Quick job submission with smart resource allocation.

    Automatically selects the compute group with most available capacity.
    If --location is specified, uses that location instead of auto-selecting.

    \b
    Examples:
        inspire run "python train.py"
        inspire run "bash train.sh" --gpus 4 --type H100
        inspire run "python train.py" --sync --watch

    \b
    With --watch:
        1. Sync code (if --sync or --watch)
        2. Create job
        3. Follow logs until completion
    """
    run_flow(
        ctx,
        command=command,
        gpus=gpus,
        gpu_type=gpu_type,
        name=name,
        sync=sync,
        watch=watch,
        priority=priority,
        location=location,
        workspace=workspace,
        workspace_id_override=workspace_id_override,
        max_time=max_time,
        image=image,
        nodes=nodes,
    )
