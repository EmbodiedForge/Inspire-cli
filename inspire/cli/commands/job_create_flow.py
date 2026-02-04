"""Job create flow for `inspire job create`."""

from __future__ import annotations

import click

from inspire.cli.commands.job_create_pipeline import submit_training_job
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils import job_submit
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.compute_group_autoselect import find_best_compute_group_location
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.workspace import select_workspace_id


def run_job_create(
    ctx: Context,
    *,
    name: str,
    resource: str,
    command: str,
    framework: str,
    priority: int,
    max_time: float,
    location: str,
    workspace: str | None,
    workspace_id_override: str | None,
    auto: bool,
    image: str,
    project: str | None,
    nodes: int,
) -> None:
    """Run the job creation flow."""
    try:
        config, _ = Config.from_files_and_env(require_target_dir=True)
        api = AuthManager.get_api(config)

        # Parse resource early for workspace routing + optional auto-selection.
        try:
            requested_gpu_type, requested_gpu_count = api.resource_manager.parse_resource_request(
                resource
            )
        except Exception as e:
            _handle_error(
                ctx, "ValidationError", f"Invalid resource spec: {e}", EXIT_VALIDATION_ERROR
            )
            return

        selected_workspace_id = select_workspace_id(
            config,
            gpu_type=requested_gpu_type.value,
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

        # Auto-select location based on GPU availability (if requested)
        if auto and not location:
            best, selected_location, selected_group_name = find_best_compute_group_location(
                api,
                gpu_type=requested_gpu_type.value,
                min_gpus=requested_gpu_count,
                include_preemptible=True,  # Count low-priority GPUs as available
                instance_count=nodes,
            )

            if not best:
                _handle_error(
                    ctx,
                    "InsufficientResources",
                    f"No {requested_gpu_type.value} compute group has at least {requested_gpu_count} available GPUs",
                    EXIT_VALIDATION_ERROR,
                )
                return

            # Fall back to group name if we can't map it.
            location = selected_location or selected_group_name

            if not ctx.json_output:
                if getattr(best, "selection_source", "") == "nodes" and getattr(
                    best, "free_nodes", 0
                ):
                    click.echo(
                        "Auto-selected: "
                        f"{selected_group_name}, {best.free_nodes} full nodes free "
                        f"({best.available_gpus} GPUs)"
                    )
                else:
                    preempt_note = (
                        f" (+{best.low_priority_gpus} preemptible)"
                        if getattr(best, "low_priority_gpus", 0) > 0
                        else ""
                    )
                    click.echo(
                        f"Auto-selected: {selected_group_name}, "
                        f"{best.available_gpus} GPUs available{preempt_note}"
                    )

        # Select project (with quota-aware fallback)
        try:
            selected, fallback_msg = job_submit.select_project_for_workspace(
                config,
                workspace_id=selected_workspace_id,
                requested=project,
            )
        except ValueError as e:
            error_type = "QuotaExceeded" if "over quota" in str(e) else "ValidationError"
            _handle_error(ctx, error_type, str(e), EXIT_CONFIG_ERROR)
            return

        selected_project_id = selected.project_id

        if not ctx.json_output:
            if fallback_msg:
                click.echo(fallback_msg)
            click.echo(f"Using project: {selected.name}{selected.get_quota_status()}")

        # Wrap in bash for consistent shell behavior and apply optional remote logging.
        try:
            submission = submit_training_job(
                api,
                config=config,
                name=name,
                command=command,
                resource=resource,
                framework=framework,
                location=location,
                project_id=selected_project_id,
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

        # Extract job ID from response
        data = submission.data
        job_id = submission.job_id

        # Output
        if ctx.json_output:
            payload = data if data else result
            click.echo(json_formatter.format_json(payload))
            return

        if job_id:
            click.echo(human_formatter.format_success(f"Job created: {job_id}"))
            click.echo(f"\nName:     {name}")
            click.echo(f"Resource: {resource}")
            if nodes > 1:
                click.echo(f"Nodes:    {nodes}")
            max_cmd_len = 80
            if len(command) > max_cmd_len:
                display_cmd = command[:max_cmd_len]
                suffix = " ... (truncated)"
            else:
                display_cmd = command
                suffix = ""
            click.echo(f"Command:  {display_cmd}{suffix}")
            if log_path:
                click.echo(f"Log file:  {log_path}")
            click.echo(f"\nCheck status with: inspire job status {job_id}")
            return

        if isinstance(result, dict):
            message = result.get("message") or "Job created (no job ID returned)"
            click.echo(human_formatter.format_success(message))
            if result.get("data"):
                click.echo(str(result["data"]))
        else:
            click.echo(human_formatter.format_success("Job created (no job ID returned)"))
            click.echo(str(result))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["run_job_create"]
