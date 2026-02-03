"""Job create command."""

from __future__ import annotations

import os
from typing import Optional

import click

from inspire.cli.commands.job_create_flow import run_job_create
from inspire.cli.context import Context, pass_context


def build_create_command(_deps) -> click.Command:  # noqa: ARG001
    @click.command("create")
    @click.option("--name", "-n", required=True, help="Job name")
    @click.option("--resource", "-r", required=True, help="Resource spec (e.g., '4xH200')")
    @click.option("--command", "-c", required=True, help="Start command")
    @click.option("--framework", default="pytorch", help="Training framework (default: pytorch)")
    @click.option(
        "--priority",
        type=int,
        default=lambda: int(os.environ.get("INSP_PRIORITY", "6")),
        help="Task priority 1-10 (default: 6, env: INSP_PRIORITY)",
    )
    @click.option(
        "--max-time", type=float, default=100.0, help="Max runtime in hours (default: 100)"
    )
    @click.option("--location", help="Preferred datacenter location")
    @click.option("--workspace", help="Workspace name (from [workspaces])")
    @click.option(
        "--workspace-id",
        "workspace_id_override",
        help="Workspace ID override (highest precedence)",
    )
    @click.option(
        "--auto/--no-auto",
        default=True,
        help="Auto-select best location based on node availability (default: auto)",
    )
    @click.option(
        "--image", default=lambda: os.environ.get("INSP_IMAGE"), help="Custom Docker image"
    )
    @click.option(
        "--project",
        "-p",
        default=lambda: os.environ.get("INSPIRE_PROJECT_ID"),
        help="Project name or ID (auto-selects first if not specified)",
    )
    @click.option(
        "--nodes",
        type=int,
        default=1,
        help="Number of nodes for multi-node training (default: 1)",
    )
    @pass_context
    def create(
        ctx: Context,
        name: str,
        resource: str,
        command: str,
        framework: str,
        priority: int,
        max_time: float,
        location: str,
        workspace: Optional[str],
        workspace_id_override: Optional[str],
        auto: bool,
        image: str,
        project: Optional[str],
        nodes: int,
    ) -> None:
        """Create a new training job.

        IMPORTANT: Always set INSPIRE_TARGET_DIR before running this command (from your laptop).
        This path should point to the shared filesystem on Bridge where training logs will be written
        (e.g., /train/logs).

        The command you provide will be wrapped to redirect stdout/stderr to this target directory:
          wrapped_command = (cd /training/code && bash train.sh) > /train/logs/job_name.log 2>&1

        When creating a job:
          - The wrapped command is sent to Inspire API
          - Inspire executes it on the Bridge machine
          - Logs are written to INSPIRE_TARGET_DIR on Bridge
          - log_path is cached in ~/.inspire/jobs.json for later retrieval

        When retrieving logs later:
          - Set INSPIRE_TARGET_DIR to the same path used during job creation
          - Use `inspire job logs <job_id>` to fetch logs via Gitea bridge

        \b
        Examples:
            export INSPIRE_TARGET_DIR="/train/logs"
            inspire job create --name "pr-123" --resource "4xH200" --command "cd /path/to/code && bash train.sh"
            inspire job create -n test -r H200 -c "python train.py" --priority 9
            inspire job create -n test -r 4xH200 -c "python train.py" --no-auto
        """
        run_job_create(
            ctx,
            name=name,
            resource=resource,
            command=command,
            framework=framework,
            priority=priority,
            max_time=max_time,
            location=location,
            workspace=workspace,
            workspace_id_override=workspace_id_override,
            auto=auto,
            image=image,
            project=project,
            nodes=nodes,
        )

    return create
