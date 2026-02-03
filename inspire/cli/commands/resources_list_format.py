"""Formatting helpers for `inspire resources list` (human output)."""

from __future__ import annotations

import click


def _format_availability_table(availability, workspace_mode: bool = False) -> None:
    title = "📊 GPU Availability (Workspace)" if workspace_mode else "📊 GPU Availability (Live)"
    scope_note = "Shows availability in your workspace only" if workspace_mode else ""

    lines = [
        "",
        title,
        "─" * 80,
    ]

    if scope_note:
        lines.append(f"{scope_note}")
        lines.append("─" * 80)

    lines.append(
        f"{'GPU Type':<12} {'Location':<25} {'Ready':<8} {'Free':<8} {'Free GPUs':<12}",
    )
    lines.append("─" * 80)

    for a in availability:
        location = a.group_name[:24]
        gpu_type = a.gpu_type[:11]

        free_gpus = a.free_gpus
        if free_gpus >= 8:
            status = ""
        elif free_gpus > 0:
            status = "⚠"
        else:
            status = "✗"

        lines.append(
            f"{gpu_type:<12} {location:<25} {a.ready_nodes:<8} {a.free_nodes:<8} "
            f"{free_gpus:<12} {status}"
        )

    lines.append("─" * 80)
    lines.append("")
    lines.append("💡 Usage:")
    lines.append('  inspire run "python train.py"              # Auto-select best group')
    lines.append('  inspire run "python train.py" --type H100   # Prefer H100')
    lines.append('  inspire run "python train.py" --gpus 4      # Use 4 GPUs')
    lines.append("")

    click.echo("\n".join(lines))


def _format_accurate_availability_table(availability) -> None:
    lines = [
        "",
        "📊 GPU Availability (Accurate Real-Time)",
        "─" * 95,
        (
            f"{'GPU Type':<22} {'Compute Group':<25} {'Available':>10} {'Used':>8} "
            f"{'Low Pri':>8} {'Total':>8}"
        ),
        "─" * 95,
    ]

    sorted_avail = sorted(availability, key=lambda x: x.available_gpus, reverse=True)

    total_available = 0
    total_used = 0
    total_low_pri = 0
    total_gpus = 0

    for a in sorted_avail:
        gpu_type = a.gpu_type[:21]
        location = a.group_name[:24]

        free_gpus = a.available_gpus
        if free_gpus >= 100:
            status = "✓"
        elif free_gpus >= 32:
            status = "○"
        elif free_gpus >= 8:
            status = "◐"
        elif free_gpus > 0:
            status = "⚠"
        else:
            status = "✗"

        lines.append(
            f"{gpu_type:<22} {location:<25} {a.available_gpus:>10} {a.used_gpus:>8} "
            f"{a.low_priority_gpus:>8} {a.total_gpus:>8} {status}"
        )

        total_available += a.available_gpus
        total_used += a.used_gpus
        total_low_pri += a.low_priority_gpus
        total_gpus += a.total_gpus

    lines.append("─" * 95)
    lines.append(
        f"{'TOTAL':<22} {'':<25} {total_available:>10} {total_used:>8} {total_low_pri:>8} "
        f"{total_gpus:>8}"
    )
    lines.append("")
    lines.append("💡 Legend:")
    lines.append("  Available = GPUs ready to use (not running any tasks)")
    lines.append("  Used      = GPUs currently running tasks")
    lines.append("  Low Pri   = GPUs running low-priority tasks (can be preempted)")
    lines.append("")
    lines.append("💡 Usage:")
    lines.append('  inspire run "python train.py"              # Auto-select best group')
    lines.append('  inspire run "python train.py" --type H100   # Prefer H100')
    lines.append('  inspire run "python train.py" --gpus 4      # Use 4 GPUs')
    lines.append("")

    click.echo("\n".join(lines))
