"""Job create command."""

from __future__ import annotations

import os
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils import job_submit
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.workspace import select_workspace_id


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
        try:
            config, _ = Config.from_files_and_env(require_target_dir=True)
            api = AuthManager.get_api(config)

            # Parse resource early for workspace routing + optional auto-selection.
            try:
                requested_gpu_type, requested_gpu_count = (
                    api.resource_manager.parse_resource_request(resource)
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
                # Use accurate browser API for resource selection
                best = browser_api_module.find_best_compute_group_accurate(
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

                # Map group_id -> location string expected by ResourceManager
                selected_group_name = best.group_name
                selected_location = None
                for group in api.resource_manager.compute_groups:
                    if group.compute_group_id == best.group_id:
                        selected_group_name = group.name
                        selected_location = group.location
                        break

                if not selected_location:
                    # Fall back to group name if we can't map it
                    selected_location = selected_group_name

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
            command = job_submit.wrap_in_bash(command)
            final_command, log_path = job_submit.build_remote_logged_command(
                config, command=command
            )

            # Convert hours to milliseconds
            max_time_ms = str(int(max_time * 3600 * 1000))

            # Create job
            create_kwargs = dict(
                name=name,
                command=final_command,
                resource=resource,
                framework=framework,
                prefer_location=location,
                project_id=selected_project_id,
                workspace_id=selected_workspace_id,
                image=image,
                task_priority=priority,
                instance_count=nodes,
                max_running_time_ms=max_time_ms,
            )
            if config.shm_size is not None:
                if config.shm_size < 1:
                    _handle_error(
                        ctx,
                        "ConfigError",
                        "Shared memory size must be >= 1 (set INSPIRE_SHM_SIZE or job.shm_size).",
                        EXIT_CONFIG_ERROR,
                    )
                    return
                create_kwargs["shm_gi"] = int(config.shm_size)

            result = api.create_training_job_smart(**create_kwargs)

            # Extract job ID from response
            data = result.get("data", {}) if isinstance(result, dict) else {}
            job_id = data.get("job_id")

            if job_id:
                job_submit.cache_created_job(
                    config,
                    job_id=job_id,
                    name=name,
                    resource=resource,
                    command=command,
                    log_path=log_path,
                )

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

    return create
