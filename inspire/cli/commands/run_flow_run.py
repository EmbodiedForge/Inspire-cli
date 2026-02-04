"""Run command flow for quick job submission (implementation)."""

from __future__ import annotations

import sys
import time
from datetime import datetime

import click

from inspire.cli.commands.job_create_pipeline import submit_training_job
from inspire.cli.commands.run_flow_select import resolve_run_resource_and_location
from inspire.cli.commands.run_flow_sync import run_sync_if_requested
from inspire.cli.commands.run_helpers import _exec_inspire_subcommand, _get_current_branch
from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils import job_submit
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config.workspaces import select_workspace_id


def run_flow(
    ctx: Context,
    *,
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
) -> None:
    """Execute the run command flow (may exit via sys.exit)."""
    run_sync_if_requested(ctx, sync=sync, watch=watch)

    try:
        config, _ = Config.from_files_and_env(require_target_dir=True)
        api = AuthManager.get_api(config)

        selected_workspace_id = select_workspace_id(
            config,
            gpu_type=gpu_type,
            explicit_workspace_id=workspace_id_override,
            explicit_workspace_name=workspace,
        )
        if not selected_workspace_id:
            _handle_error(
                ctx,
                "ConfigError",
                "No workspace_id configured for GPU workloads. Set [workspaces].gpu or INSPIRE_WORKSPACE_ID.",
                EXIT_CONFIG_ERROR,
            )
            return

        resource_str, location = resolve_run_resource_and_location(
            ctx,
            api=api,
            gpus=gpus,
            gpu_type=gpu_type,
            location=location,
            nodes=nodes,
        )

        # Generate job name if not provided
        if not name:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            branch = _get_current_branch()
            branch_suffix = f"-{branch}" if branch else ""
            name = f"run-{timestamp}{branch_suffix}"

        # Create job
        if not ctx.json_output:
            click.echo(f"Creating job '{name}'...")

        # Brief delay before job creation to avoid API rate limits
        time.sleep(0.5)

        try:
            selected_project, fallback_msg = job_submit.select_project_for_workspace(
                config,
                workspace_id=selected_workspace_id,
                requested=None,
            )
        except ValueError as e:
            error_type = "QuotaExceeded" if "over quota" in str(e) else "ValidationError"
            _handle_error(ctx, error_type, str(e), EXIT_CONFIG_ERROR)
            return
        project_id = selected_project.project_id

        if not ctx.json_output:
            if fallback_msg:
                click.echo(fallback_msg)
            click.echo(
                f"Using project: {selected_project.name}{selected_project.get_quota_status()}"
            )

        try:
            submission = submit_training_job(
                api,
                config=config,
                name=name,
                command=command,
                resource=resource_str,
                framework="pytorch",
                location=location,
                project_id=project_id,
                workspace_id=selected_workspace_id,
                image=image,
                priority=priority,
                nodes=nodes,
                max_time_hours=max_time,
            )
        except ValueError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        command = submission.wrapped_command
        log_path = submission.log_path
        result = submission.result

        # Extract job ID
        data = submission.data
        job_id = submission.job_id

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

        # Watch if requested
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


__all__ = ["run_flow"]
