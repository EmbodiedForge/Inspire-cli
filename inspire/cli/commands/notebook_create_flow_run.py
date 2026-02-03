"""Notebook creation flow (implementation for `inspire notebook create`)."""

from __future__ import annotations

import uuid
from typing import Optional

import click

from inspire.cli.commands.notebook_create_flow_compute_group import resolve_notebook_compute_group
from inspire.cli.commands.notebook_create_flow_image import resolve_notebook_image
from inspire.cli.commands.notebook_create_flow_post import (
    maybe_start_keepalive,
    maybe_wait_for_running,
)
from inspire.cli.commands.notebook_create_flow_project import resolve_notebook_project
from inspire.cli.commands.notebook_create_flow_quota import resolve_notebook_quota
from inspire.cli.commands.notebook_create_flow_submit import create_notebook_and_report
from inspire.cli.commands.notebook_create_flow_workspace import resolve_notebook_workspace_id
from inspire.cli.commands.notebook_create_helpers import (
    format_resource_display,
    parse_resource_string,
)
from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import load_config, require_web_session, resolve_json_output


def run_notebook_create(
    ctx: Context,
    *,
    name: Optional[str],
    workspace: Optional[str],
    workspace_id: Optional[str],
    resource: str,
    project: Optional[str],
    image: Optional[str],
    shm_size: Optional[int],
    auto_stop: bool,
    auto: bool,
    wait: bool,
    keepalive: bool,
    json_output: bool,
) -> None:
    """Run the notebook creation flow."""
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Creating notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )
    config = load_config(ctx)
    if shm_size is None:
        shm_size = config.shm_size if config.shm_size is not None else 32
    if shm_size < 1:
        _handle_error(ctx, "ValidationError", "Shared memory size must be >= 1.", EXIT_CONFIG_ERROR)
        return

    try:
        gpu_count, gpu_pattern, cpu_count = parse_resource_string(resource)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    requested_cpu_count = cpu_count
    resource_display = format_resource_display(gpu_count, gpu_pattern, requested_cpu_count)

    workspace_id = resolve_notebook_workspace_id(
        ctx,
        config=config,
        session=session,
        workspace=workspace,
        workspace_id=workspace_id,
        gpu_count=gpu_count,
        gpu_pattern=gpu_pattern,
    )
    if not workspace_id:
        return

    compute_group = resolve_notebook_compute_group(
        ctx,
        session=session,
        workspace_id=workspace_id,
        gpu_count=gpu_count,
        gpu_pattern=gpu_pattern,
        requested_cpu_count=requested_cpu_count,
        auto=auto,
        json_output=json_output,
    )
    if not compute_group:
        return

    logic_compute_group_id, selected_gpu_type, gpu_pattern, resource_display = compute_group

    try:
        schedule = browser_api_module.get_notebook_schedule(
            workspace_id=workspace_id, session=session
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch notebook schedule: {e}", EXIT_API_ERROR)
        return

    quota_selection = resolve_notebook_quota(
        ctx,
        schedule=schedule,
        gpu_count=gpu_count,
        gpu_pattern=gpu_pattern,
        requested_cpu_count=requested_cpu_count,
        selected_gpu_type=selected_gpu_type,
    )
    if not quota_selection:
        return

    quota_id, cpu_count, memory_size, selected_gpu_type, resource_display = quota_selection

    try:
        projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch projects: {e}", EXIT_API_ERROR)
        return

    if not projects:
        _handle_error(
            ctx, "ConfigError", "No projects available in this workspace", EXIT_CONFIG_ERROR
        )
        return

    selected_project = resolve_notebook_project(
        ctx,
        projects=projects,
        project=project,
        json_output=json_output,
    )
    if not selected_project:
        return

    try:
        images = browser_api_module.list_images(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch images: {e}", EXIT_API_ERROR)
        return

    if not images:
        _handle_error(ctx, "ConfigError", "No images available", EXIT_CONFIG_ERROR)
        return

    selected_image = resolve_notebook_image(
        ctx,
        images=images,
        image=image,
        json_output=json_output,
    )
    if not selected_image:
        return

    if not json_output:
        click.echo(f"Using image: {selected_image.name}")

    if not name:
        name = f"notebook-{uuid.uuid4().hex[:8]}"
        if not json_output:
            click.echo(f"Generated name: {name}")

    notebook_id = create_notebook_and_report(
        ctx,
        name=name,
        resource_display=resource_display,
        selected_project=selected_project,
        selected_image=selected_image,
        logic_compute_group_id=logic_compute_group_id,
        quota_id=quota_id,
        selected_gpu_type=selected_gpu_type,
        gpu_count=gpu_count,
        cpu_count=cpu_count,
        memory_size=memory_size,
        shm_size=shm_size,
        auto_stop=auto_stop,
        workspace_id=workspace_id,
        session=session,
        json_output=json_output,
    )
    if not notebook_id:
        return

    if not maybe_wait_for_running(
        ctx,
        notebook_id=notebook_id,
        session=session,
        wait=wait,
        keepalive=keepalive,
        json_output=json_output,
        timeout=600,
    ):
        return

    maybe_start_keepalive(
        ctx,
        notebook_id=notebook_id,
        session=session,
        keepalive=keepalive,
        gpu_count=gpu_count,
        json_output=json_output,
    )

    if not json_output:
        click.echo(f"\nUse 'inspire notebook status {notebook_id}' to check status.")


__all__ = ["run_notebook_create"]
