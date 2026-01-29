"""Run command - Quick job submission with smart resource allocation.

Usage:
    inspire run "python train.py"
    inspire run "bash train.sh" --gpus 4 --type H100
    inspire run "python train.py" --sync --watch
"""

import os
import sys
import subprocess
import time
from datetime import datetime
import shutil

import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_SUCCESS,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.utils.config import Config, ConfigError, build_env_exports
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.browser_api import find_best_compute_group_accurate
from inspire.cli.formatters import json_formatter, human_formatter


def _get_current_branch() -> str | None:
    """Get the current git branch name."""
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
    """Check if there are uncommitted changes."""
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
    """Find the inspire CLI executable in PATH."""
    return shutil.which("inspire")


def _run_inspire_subcommand(args: list[str]) -> int:
    """Run an inspire subcommand as a subprocess."""
    exe = _get_inspire_executable()
    if not exe:
        raise RuntimeError("Cannot find 'inspire' executable in PATH")

    proc = subprocess.run([exe, *args])
    return proc.returncode


def _exec_inspire_subcommand(args: list[str]) -> None:
    """Exec (replace process) with an inspire subcommand."""
    exe = _get_inspire_executable()
    if not exe:
        raise RuntimeError("Cannot find 'inspire' executable in PATH")

    os.execv(exe, [exe, *args])


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
    name: str,
    sync: bool,
    watch: bool,
    priority: int,
    location: str,
    max_time: float,
    image: str,
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
    # Step 1: Sync if requested
    if sync or watch:
        if not ctx.json_output:
            click.echo("Syncing code...")

        # Check for uncommitted changes (avoid sync interactive prompts)
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

        # Brief delay after sync to avoid API rate limits
        time.sleep(0.5)

    try:
        config, _ = Config.from_files_and_env(require_target_dir=True)
        api = AuthManager.get_api(config)

        # Step 2: Auto-select compute group
        if location:
            # Use user-specified location
            resource_str = f"{gpus}x{gpu_type}"
        else:
            # Smart selection using accurate browser API
            if not ctx.json_output:
                click.echo("Checking GPU availability...")

            best = find_best_compute_group_accurate(
                gpu_type=gpu_type,
                min_gpus=gpus,
                include_preemptible=True,  # Count low-priority GPUs as available
                instance_count=nodes,
            )

            if not best:
                if ctx.json_output:
                    click.echo(
                        json_formatter.format_json_error(
                            "InsufficientResources",
                            f"No compute groups with at least {gpus} {gpu_type} GPUs available",
                            EXIT_VALIDATION_ERROR,
                        )
                    )
                else:
                    click.echo(
                        human_formatter.format_error(
                            f"No compute groups with at least {gpus} {gpu_type} GPUs available",
                            hint="Try different GPU type or fewer GPUs. Run 'inspire resources list' to see availability.",
                        ),
                        err=True,
                    )
                sys.exit(EXIT_VALIDATION_ERROR)

            # Build resource string
            resource_str = f"{gpus}x{gpu_type}"

            # Map selected group_id -> location string expected by ResourceManager
            selected_group_name = best.group_name
            selected_location = None
            for group in api.resource_manager.compute_groups:
                if group.compute_group_id == best.group_id:
                    selected_group_name = group.name
                    selected_location = group.location
                    break

            if selected_location:
                location = selected_location

            if not ctx.json_output:
                if best.selection_source == "nodes" and best.free_nodes:
                    click.echo(
                        "Auto-selected: "
                        f"{selected_group_name}, {best.free_nodes} full nodes free "
                        f"({best.available_gpus} GPUs)"
                    )
                else:
                    preempt_note = (
                        f" (+{best.low_priority_gpus} preemptible)"
                        if best.low_priority_gpus > 0
                        else ""
                    )
                    click.echo(
                        f"Auto-selected: {selected_group_name}, {best.available_gpus} GPUs available{preempt_note}"
                    )

        # Step 3: Generate job name if not provided
        if not name:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            branch = _get_current_branch()
            branch_suffix = f"-{branch}" if branch else ""
            name = f"run-{timestamp}{branch_suffix}"

        # Step 4: Create job
        if not ctx.json_output:
            click.echo(f"Creating job '{name}'...")

        # Brief delay before job creation to avoid API rate limits
        time.sleep(0.5)

        # Import job creation logic
        from inspire.cli.commands.job import _wrap_in_bash
        from inspire.cli.utils.job_cache import JobCache

        # Wrap command in bash
        command = _wrap_in_bash(command)

        # Prepend remote_env exports to command
        env_exports = build_env_exports(config.remote_env)

        # Set up log path
        log_path = None
        if config.target_dir:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            log_dir = os.path.join(config.target_dir, ".inspire")
            log_filename = f"training_master_{ts}.log"
            log_path = os.path.join(log_dir, log_filename)
            final_command = f'{env_exports}mkdir -p "{log_dir}" && ( {command} ) > "{log_path}" 2>&1'
        else:
            final_command = f"{env_exports}{command}" if env_exports else command

        # Convert hours to milliseconds
        max_time_ms = str(int(max_time * 3600 * 1000))

        # Create job via API
        result = api.create_training_job_smart(
            name=name,
            command=final_command,
            resource=resource_str,
            framework="pytorch",
            prefer_location=location,
            image=image,
            task_priority=priority,
            instance_count=nodes,
            max_running_time_ms=max_time_ms,
        )

        # Extract job ID
        data = result.get("data", {}) if isinstance(result, dict) else {}
        job_id = data.get("job_id")

        if not job_id:
            if ctx.json_output:
                click.echo(json_formatter.format_json(data if data else result))
            else:
                if isinstance(result, dict):
                    message = result.get("message") or "Job created (no job ID returned)"
                    click.echo(human_formatter.format_success(message))
                    if result.get("data"):
                        click.echo(str(result["data"]))
                else:
                    click.echo(human_formatter.format_success("Job created"))
                    click.echo(str(result))
            sys.exit(EXIT_SUCCESS)

        # Save to cache
        cache = JobCache(config.get_expanded_cache_path())
        cache.add_job(
            job_id=job_id,
            name=name,
            resource=resource_str,
            command=command,
            status="PENDING",
            log_path=log_path,
        )

        # Output
        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
        else:
            click.echo(human_formatter.format_success(f"Job created: {job_id}"))
            click.echo(f"\nName:     {name}")
            click.echo(f"Resource: {resource_str}")
            if nodes > 1:
                click.echo(f"Nodes:    {nodes}")
            click.echo(f"Command:  {command[:80]}{'...' if len(command) > 80 else ''}")
            if log_path:
                click.echo(f"Log file: {log_path}")
            click.echo(f"\nCheck status with: inspire job status {job_id}")

        # Step 5: Watch if requested
        if watch:
            if ctx.json_output:
                # For JSON mode, just return the job info
                sys.exit(EXIT_SUCCESS)

            click.echo("\nFollowing logs...")
            try:
                _exec_inspire_subcommand(["job", "logs", job_id, "--follow"])
            except Exception as e:
                click.echo(f"Failed to start log follow: {e}", err=True)
                click.echo(f"You can still run: inspire job logs {job_id} --follow")
                sys.exit(EXIT_GENERAL_ERROR)

        sys.exit(EXIT_SUCCESS)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_GENERAL_ERROR)


def _handle_error(ctx: Context, error_type: str, message: str, exit_code: int) -> None:
    """Handle and format errors consistently."""
    if ctx.json_output:
        click.echo(json_formatter.format_json_error(error_type, message, exit_code), err=True)
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(exit_code)
