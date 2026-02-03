"""Compute-group selection for `inspire notebook create`."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.web_session import WebSession

from .helpers import format_resource_display, match_gpu_type


def resolve_notebook_compute_group(
    ctx: Context,
    *,
    session: WebSession,
    workspace_id: str,
    gpu_count: int,
    gpu_pattern: str,
    requested_cpu_count: Optional[int],
    auto: bool,
    json_output: bool,
) -> tuple[str, str, str, str] | None:
    """Pick a compute group and return (group_id, selected_gpu_type, gpu_pattern, resource_display)."""
    auto_selected_group = None
    auto_selected_gpu_type = ""

    if auto and gpu_count > 0:
        filter_gpu_type = None if gpu_pattern == "GPU" else gpu_pattern

        try:
            best = browser_api_module.find_best_compute_group_accurate(
                gpu_type=filter_gpu_type,
                min_gpus=gpu_count,
                include_preemptible=True,
                prefer_full_nodes=True,
            )

            if best:
                auto_selected_group = best
                if gpu_pattern == "GPU":
                    gpu_pattern = best.gpu_type or "GPU"
                auto_selected_gpu_type = best.gpu_type or ""

                if not json_output:
                    if best.selection_source == "nodes" and best.free_nodes:
                        click.echo(
                            f"Auto-selected: {best.group_name}, "
                            f"{best.free_nodes} full node(s) free ({best.available_gpus} GPUs)"
                        )
                    else:
                        click.echo(
                            f"Auto-selected: {best.group_name}, {best.available_gpus} GPUs available"
                        )
            elif gpu_pattern == "GPU":
                _handle_error(
                    ctx,
                    "AvailabilityError",
                    f"No compute group has {gpu_count} GPUs available",
                    EXIT_CONFIG_ERROR,
                )
                return None
        except Exception as e:
            if not json_output:
                click.echo(f"Warning: Auto-select failed ({e}), using manual selection", err=True)
            auto_selected_group = None

    resource_display = format_resource_display(gpu_count, gpu_pattern, requested_cpu_count)

    if not json_output:
        click.echo(f"Creating notebook with {resource_display}...")

    try:
        compute_groups = browser_api_module.list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Error fetching compute groups: {e}", EXIT_API_ERROR)
        return None

    selected_group = None
    selected_gpu_type = ""

    if auto_selected_group:
        for group in compute_groups:
            if group.get("logic_compute_group_id") == auto_selected_group.group_id:
                selected_group = group
                selected_gpu_type = auto_selected_gpu_type
                break

        if not selected_group:
            for group in compute_groups:
                gpu_stats_list = group.get("gpu_type_stats", [])
                for gpu_stats in gpu_stats_list:
                    gpu_info = gpu_stats.get("gpu_info", {})
                    gpu_type_display = gpu_info.get("gpu_type_display", "")
                    if match_gpu_type(auto_selected_group.gpu_type, gpu_type_display):
                        selected_group = group
                        selected_gpu_type = gpu_info.get("gpu_type", "")
                        break
                if selected_group:
                    break

    if not selected_group:
        for group in compute_groups:
            gpu_stats_list = group.get("gpu_type_stats", [])
            for gpu_stats in gpu_stats_list:
                gpu_info = gpu_stats.get("gpu_info", {})
                gpu_type_display = gpu_info.get("gpu_type_display", "")
                if match_gpu_type(gpu_pattern, gpu_type_display):
                    selected_group = group
                    selected_gpu_type = gpu_info.get("gpu_type", "")
                    break
            if selected_group:
                break

    if not selected_group and gpu_count == 0:
        for group in compute_groups:
            if not group.get("gpu_type_stats"):
                selected_group = group
                selected_gpu_type = ""
                break

    if not selected_group:
        available_types: set[str] = set()
        for group in compute_groups:
            for stats in group.get("gpu_type_stats", []):
                gpu_type = stats.get("gpu_info", {}).get("gpu_type_display", "Unknown")
                if gpu_type:
                    available_types.add(gpu_type)
        if not available_types and gpu_count == 0:
            available_types.add("CPU")

        hint = None
        if available_types:
            formatted = "\n".join(f"  - {gpu_type}" for gpu_type in sorted(available_types))
            hint = f"Available resource types:\n{formatted}"

        _handle_error(
            ctx,
            "ValidationError",
            f"No compute group found with resource type matching '{gpu_pattern}'",
            EXIT_CONFIG_ERROR,
            hint=hint,
        )
        return None

    logic_compute_group_id = selected_group.get("logic_compute_group_id")
    if not logic_compute_group_id:
        _handle_error(
            ctx,
            "APIError",
            "Selected compute group is missing logic_compute_group_id",
            EXIT_API_ERROR,
        )
        return None

    return logic_compute_group_id, selected_gpu_type, gpu_pattern, resource_display


__all__ = ["resolve_notebook_compute_group"]
