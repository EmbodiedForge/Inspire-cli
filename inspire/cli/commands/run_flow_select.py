"""Selection helpers for `inspire run`."""

from __future__ import annotations

import sys

import click

from inspire.cli.context import Context, EXIT_VALIDATION_ERROR
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.compute_group_autoselect import find_best_compute_group_location


def resolve_run_resource_and_location(
    ctx: Context,
    *,
    api,  # noqa: ANN001
    gpus: int,
    gpu_type: str,
    location: str | None,
    nodes: int,
) -> tuple[str, str | None]:
    """Return (resource_str, location). May sys.exit on handled error."""
    if location:
        return f"{gpus}x{gpu_type}", location

    if not ctx.json_output:
        click.echo("Checking GPU availability...")

    best, selected_location, selected_group_name = find_best_compute_group_location(
        api,
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
                    hint=(
                        "Try different GPU type or fewer GPUs. Run 'inspire resources list' "
                        "to see availability."
                    ),
                ),
                err=True,
            )
        sys.exit(EXIT_VALIDATION_ERROR)

    resource_str = f"{gpus}x{gpu_type}"

    # For `inspire run`, location is best-effort (may remain None if mapping fails).
    if selected_location:
        location = selected_location

    if not ctx.json_output:
        if getattr(best, "selection_source", "") == "nodes" and getattr(best, "free_nodes", 0):
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

    return resource_str, location


__all__ = ["resolve_run_resource_and_location"]
