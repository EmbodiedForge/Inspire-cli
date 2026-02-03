"""Rendering helpers for `inspire resources list --watch`."""

from __future__ import annotations

import os

import click


def _progress_bar(current: int, total: int, width: int = 20) -> str:
    if total == 0:
        return "░" * width
    filled = int(width * current / total)
    return "█" * filled + "░" * (width - filled)


def render_nodes_display(
    availability: list,
    *,
    phase: str,
    timestamp: str,
    interval: int,
    progress_state: dict[str, int],
) -> None:
    os.system("clear")

    if phase == "fetching":
        fetched = progress_state["fetched"]
        total = progress_state["total"] or 1
        bar = _progress_bar(fetched, total)
        if total > 1:
            click.echo(f"🔄 [{bar}] Fetching {fetched}/{total} nodes...\n")
        else:
            click.echo(f"🔄 [{bar}] Fetching availability...\n")
    else:
        bar = _progress_bar(1, 1)
        click.echo(f"✅ [{bar}] Updated at {timestamp} (Workspace) (interval: {interval}s)\n")

    if not availability:
        if phase != "fetching":
            click.echo("No GPU resources found")
        return

    click.echo("─" * 60)
    click.echo(f"{'GPU':<6} {'Location':<24} {'Ready':>8} {'Free':>8} {'GPUs':>8}")
    click.echo("─" * 60)

    total_free = 0
    for a in availability:
        location = a.group_name[:23]
        gpu = a.gpu_type[:5]
        free_gpus = a.free_gpus
        total_free += free_gpus

        if free_gpus >= 64:
            indicator = "🟢"
        elif free_gpus >= 16:
            indicator = "🟡"
        elif free_gpus > 0:
            indicator = "🟠"
        else:
            indicator = "🔴"

        click.echo(
            f"{gpu:<6} {location:<24} {a.ready_nodes:>8} {a.free_nodes:>8} "
            f"{free_gpus:>8} {indicator}"
        )

    click.echo("─" * 60)
    click.echo(f"{'Total':<6} {'':<24} {'':>8} {'':>8} {total_free:>8}")
    click.echo("")
    click.echo("Ctrl+C to stop")


def render_accurate_display(
    availability: list,
    *,
    phase: str,
    timestamp: str,
    interval: int,
) -> None:
    os.system("clear")

    if phase == "fetching":
        click.echo("🔄 Fetching accurate availability...\n")
    else:
        click.echo(f"✅ Updated at {timestamp} (Accurate) (interval: {interval}s)\n")

    if not availability:
        if phase != "fetching":
            click.echo("No GPU resources found")
        return

    lines = [
        "─" * 95,
        (
            f"{'GPU Type':<22} {'Compute Group':<25} {'Available':>10} "
            f"{'Used':>8} {'Low Pri':>8} {'Total':>8}"
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
        f"{'TOTAL':<22} {'':<25} {total_available:>10} {total_used:>8} "
        f"{total_low_pri:>8} {total_gpus:>8}"
    )
    lines.append("")
    lines.append("Ctrl+C to stop")

    click.echo("\n".join(lines))


def render_display(
    *,
    mode: str,
    availability: list,
    phase: str,
    timestamp: str,
    interval: int,
    progress_state: dict[str, int],
) -> None:
    if mode == "nodes":
        render_nodes_display(
            availability,
            phase=phase,
            timestamp=timestamp,
            interval=interval,
            progress_state=progress_state,
        )
    else:
        render_accurate_display(availability, phase=phase, timestamp=timestamp, interval=interval)


__all__ = ["render_display"]
