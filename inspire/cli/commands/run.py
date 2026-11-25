"""Compatibility adapter for ``job create`` with optional sync and log following."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime

import click

from inspire.cli.commands.job.job_create import run_job_create
from inspire.cli.context import Context, EXIT_GENERAL_ERROR, EXIT_SUCCESS, pass_context
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_error, emit_info, emit_warning
from inspire.config.workspaces import WORKSPACE_ALIAS_HELP


def _get_current_branch() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _check_uncommitted_changes() -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _get_inspire_executable() -> str | None:
    return shutil.which("inspire")


def _run_inspire_subcommand(args: list[str]) -> int:
    exe = _get_inspire_executable()
    if not exe:
        raise RuntimeError("Cannot find 'inspire' executable in PATH")
    return subprocess.run([exe, *args]).returncode


def _exec_inspire_subcommand(args: list[str]) -> None:
    exe = _get_inspire_executable()
    if not exe:
        raise RuntimeError("Cannot find 'inspire' executable in PATH")
    os.execv(exe, [exe, *args])


def _run_sync_if_requested(ctx: Context, *, sync: bool, watch: bool) -> None:
    if not (sync or watch):
        return
    if ctx.debug and not ctx.json_output:
        emit_info(ctx, "Syncing code...")
    if _check_uncommitted_changes():
        _handle_error(
            ctx,
            "ValidationError",
            "Uncommitted changes detected. Commit or stash first.",
            EXIT_GENERAL_ERROR,
        )
    try:
        exit_code = _run_inspire_subcommand(["sync"])
    except Exception as e:
        _handle_error(ctx, "SyncError", f"Failed to run sync: {e}", EXIT_GENERAL_ERROR)
    if exit_code != EXIT_SUCCESS:
        _handle_error(ctx, "SyncError", "Code sync failed", EXIT_GENERAL_ERROR)
    time.sleep(0.5)


def _run_flow(
    ctx: Context,
    *,
    command: str,
    gpus: int,
    gpu_type: str,
    name: str | None,
    sync: bool,
    watch: bool,
    priority: int | None,
    location: str | None,
    workspace: str | None,
    workspace_id_override: str | None,
    max_time: float,
    image: str | None,
    nodes: int,
    project: str | None,
    fault_tolerant: bool | None,
    fault_tolerance_max_retry: int | None,
    dry_run: bool = False,
) -> None:
    if dry_run and (sync or watch) and not ctx.json_output:
        emit_warning(ctx, "--dry-run: skipping code sync.")
    if not dry_run:
        _run_sync_if_requested(ctx, sync=sync, watch=watch)

    if not name:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = _get_current_branch()
        name = f"run-{timestamp}{f'-{branch}' if branch else ''}"

    submission = run_job_create(
        ctx,
        name=name,
        resource=f"{gpus}x{gpu_type}",
        command=command,
        framework="pytorch",
        priority=priority,
        max_time=max_time,
        location=location or "",
        group=None,
        quota=None,
        workspace=workspace,
        workspace_id_override=workspace_id_override,
        auto=location is None,
        image=image,
        project=project,
        nodes=nodes,
        fault_tolerant=fault_tolerant,
        fault_tolerance_max_retry=fault_tolerance_max_retry,
        keep_alive_on_success=None,
        keep_alive_on_fail=None,
        dry_run=dry_run,
    )
    if dry_run or not watch or ctx.json_output or not submission or not submission.job_id:
        return
    if ctx.debug:
        emit_info(ctx, "Fetching logs...")
    try:
        _exec_inspire_subcommand(["job", "logs", submission.job_id])
    except Exception as e:
        emit_error(
            ctx,
            error_type="LogFollowError",
            message=f"Failed to read logs: {e}",
            exit_code=EXIT_GENERAL_ERROR,
            hint=f"You can still run: inspire job logs {submission.job_id}",
        )


@click.command()
@click.argument("command")
@click.option("--gpus", "-g", type=int, default=8, help="Number of GPUs (default: 8)")
@click.option(
    "--type",
    "gpu_type",
    type=click.Choice(["H100", "H200"], case_sensitive=False),
    default="H200",
    help="GPU type (default: H200)",
)
@click.option("--name", "-n", help="Job name (auto-generated if not specified)")
@click.option("--sync", "-s", is_flag=True, help="Sync code before running")
@click.option("--watch", "-w", is_flag=True, help="Sync, run, then print logs")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=None,
    help="Task priority 1-10 (default from config [job].priority or [defaults].priority or 6). "
    "The selected Scheduling Offer determines any cap.",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name or ID (default from config [job].project_id or [defaults].project_order)",
)
@click.option("--location", help="Preferred datacenter location (overrides auto-selection)")
@click.option("--workspace", help=WORKSPACE_ALIAS_HELP)
@click.option(
    "--workspace-id",
    "workspace_id_override",
    help="Workspace ID override (escape hatch; highest precedence)",
)
@click.option("--max-time", type=float, default=100.0, help="Max runtime in hours (default: 100)")
@click.option(
    "--image",
    default=None,
    help="Custom Docker image (default from config [job].image or [defaults].image)",
)
@click.option(
    "--nodes",
    type=click.IntRange(min=1),
    default=1,
    help="Number of nodes for multi-node training (default: 1)",
)
@click.option(
    "--fault-tolerant/--no-fault-tolerant",
    default=None,
    help="Auto-restart on failure/preemption (auto-enabled for LOW projects or priority < 4)",
)
@click.option(
    "--fault-tolerance-max-retry",
    type=click.IntRange(1, 2000),
    default=None,
    help="Maximum platform runs when fault tolerance is enabled (default: 100)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve the launch and print the platform request (secrets redacted) without creating a job",
)
@json_option
@pass_context
def run(
    ctx: Context,
    command: str,
    gpus: int,
    gpu_type: str,
    name: str | None,
    sync: bool,
    watch: bool,
    priority: int | None,
    project: str | None,
    location: str | None,
    workspace: str | None,
    workspace_id_override: str | None,
    max_time: float,
    image: str | None,
    nodes: int,
    fault_tolerant: bool | None,
    fault_tolerance_max_retry: int | None,
    dry_run: bool = False,
    json_output: bool = False,
) -> None:
    """Submit through the job-create compatibility path, optionally syncing and following logs.

    \b
    Examples:
        inspire run "python train.py" --sync --watch
        inspire run "python train.py" --dry-run
    """
    resolve_json_output(ctx, json_output)
    _run_flow(
        ctx,
        command=command,
        gpus=gpus,
        gpu_type=gpu_type,
        name=name,
        sync=sync,
        watch=watch,
        priority=priority,
        project=project,
        location=location,
        workspace=workspace,
        workspace_id_override=workspace_id_override,
        max_time=max_time,
        image=image,
        nodes=nodes,
        fault_tolerant=fault_tolerant,
        fault_tolerance_max_retry=fault_tolerance_max_retry,
        dry_run=dry_run,
    )


__all__ = ["run"]
