"""Resources nodes command (full free nodes per group)."""

from __future__ import annotations

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_AUTH_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config
from inspire.platform.web.resources import fetch_resource_availability
from inspire.platform.web.session import SessionExpiredError


@click.command("nodes")
@click.option("--group", help="Filter by compute group name (partial match)")
@click.option(
    "--resource",
    help="Filter by platform-reported resource class, for example 4090, H100, H200, or CPU",
)
@json_option
@pass_context
def list_nodes(
    ctx: Context, group: str | None, resource: str | None, json_output: bool = False
) -> None:
    """Show full-free node counts per compute group.

    This uses workspace?Action=ListNodeDimension, which returns per-node
    CPU/memory/GPU availability and task counts. There is no aggregate
    fallback for free capacity: if the probe is denied or fails, node counts
    and capacity fields are null/n/a and ``availability_source`` is
    ``unavailable``. ``Low Pri`` is a separate advisory current field
    (``low_priority_source=aggregate_advisory``), not free capacity.

    \b
    Examples:
        inspire resources nodes
        inspire resources nodes --group H200
        inspire resources nodes --resource 4090
    """

    json_output = resolve_json_output(ctx, json_output)
    try:
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
        except Exception:
            config = None
        from inspire.cli.commands.resources.resources_list import (
            _enumerate_accessible_workspace_ids,
        )
        from inspire.platform.web.session import get_web_session

        workspace_ids = _enumerate_accessible_workspace_ids(get_web_session(require_workspace=True))
        aliases_by_id: dict[str, list[str]] = {}

        probe_errors: list[str] = []

        def _record_probe_error(workspace_id: str, exc: Exception) -> None:
            probe_errors.append(f"{workspace_id}: {exc}")

        availability = fetch_resource_availability(
            config=config,
            known_only=False,
            workspace_ids=workspace_ids,
            workspace_aliases_by_id=aliases_by_id,
            workspace_error_callback=_record_probe_error,
            gpu_type_filter=resource,
        )

        sources = {str(getattr(item, "availability_source", "") or "") for item in availability}
        node_blind = bool(availability) and sources == {"unavailable"}
        has_unavailable = "unavailable" in sources
        blind_note = None
        if has_unavailable:
            blind_note = (
                "node-level visibility unavailable; no aggregate fallback is used "
                f"({probe_errors[0] if probe_errors else 'node probe failed'}); "
                "capacity fields are null/n/a, not zero"
            )

        filtered: list[dict] = []
        group_lower = (group or "").lower()
        for item in availability:
            name = item.group_name or item.group_id[-12:]
            if group_lower and group_lower not in name.lower():
                continue
            nodes_visible = item.availability_source in {"nodes", "nodes-gpu"}
            available_gpus = item.gpu_available_from_nodes if nodes_visible else None
            low_priority_gpus = item.low_priority_gpus if nodes_visible else None

            def node_count(value: int, visible: bool = nodes_visible) -> int | None:
                return value if visible else None

            filtered.append(
                {
                    "group_id": item.group_id,
                    "group_name": name,
                    "gpu_type": item.gpu_type,
                    "gpu_per_node": item.gpu_per_node,
                    "total_nodes": node_count(item.total_nodes),
                    "ready_nodes": node_count(item.ready_nodes),
                    "free_nodes": node_count(item.free_nodes),
                    "full_free_nodes": node_count(item.free_nodes),
                    "gpu_full_free_nodes": node_count(item.gpu_full_free_nodes),
                    "schedulable_full_nodes": node_count(item.schedulable_full_nodes),
                    "partial_free_nodes": node_count(item.partial_free_nodes),
                    "low_priority_nodes_estimate": node_count(item.low_priority_nodes_estimate),
                    "full_free_gpus": node_count(item.free_gpus),
                    "available_gpus": available_gpus,
                    "low_priority_gpus": low_priority_gpus,
                    "low_priority_source": (
                        getattr(item, "low_priority_source", None) if nodes_visible else None
                    ),
                    "gpu_available_from_nodes": node_count(item.gpu_available_from_nodes),
                    "availability_source": item.availability_source,
                    "fallback_reason": item.fallback_reason,
                    "workspace_ids": item.workspace_ids,
                    "workspace_aliases": item.workspace_aliases,
                }
            )

        filtered.sort(
            key=lambda x: (
                x["free_nodes"] if x["free_nodes"] is not None else -1,
                x["gpu_full_free_nodes"] if x["gpu_full_free_nodes"] is not None else -1,
                x["available_gpus"],
            ),
            reverse=True,
        )

        def total(field: str) -> int | None:
            values = [row[field] for row in filtered]
            return None if any(value is None for value in values) else sum(values)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "groups": filtered,
                        "total_full_free_nodes": total("free_nodes"),
                        "total_gpu_full_free_nodes": total("gpu_full_free_nodes"),
                        "total_schedulable_full_nodes": total("schedulable_full_nodes"),
                        "total_available_gpus": total("available_gpus"),
                        "total_low_priority_gpus": total("low_priority_gpus"),
                        "node_visibility": "unavailable" if node_blind else "nodes",
                        "node_visibility_note": blind_note,
                        "probe_errors": probe_errors,
                        "resource": resource,
                    }
                )
            )
            return

        click.echo("")
        if blind_note:
            click.echo(f"⚠️  {blind_note}")
        click.echo("📊 GPU Node Availability by Compute Group")
        click.echo("─" * 112)
        click.echo(
            f"{'Group':<25} {'Free':>7} {'GPU Free':>9} {'LowPri Nodes':>12} "
            f"{'Idle GPUs':>10} {'LowPri GPUs':>11} {'Ready':>8} {'Total':>8} "
            f"{'Source':>12}"
        )
        click.echo("─" * 112)

        def display(value: int | None) -> str:
            return "n/a" if value is None else str(value)

        for row in filtered:
            name = row["group_name"][:24]
            full_free = row["free_nodes"]
            source = row["availability_source"]

            if full_free is None:
                indicator = "-"
            elif full_free >= 10:
                indicator = "🟢"
            elif full_free >= 3:
                indicator = "🟡"
            elif full_free > 0:
                indicator = "🟠"
            else:
                indicator = "🔴"

            click.echo(
                f"{name:<25} {display(full_free):>7} "
                f"{display(row['gpu_full_free_nodes']):>9} "
                f"{display(row['low_priority_nodes_estimate']):>12} "
                f"{display(row['available_gpus']):>10} "
                f"{display(row['low_priority_gpus']):>11} "
                f"{display(row['ready_nodes']):>8} {display(row['total_nodes']):>8} "
                f"{source:>12} {indicator}"
            )

        click.echo("─" * 112)
        click.echo(
            f"{'TOTAL':<25} {display(total('free_nodes')):>7} "
            f"{display(total('gpu_full_free_nodes')):>9} "
            f"{display(total('low_priority_nodes_estimate')):>12} "
            f"{display(total('available_gpus')):>10} "
            f"{display(total('low_priority_gpus')):>11}"
        )
        click.echo("")
        click.echo("n/a = node-level data unavailable; capacity has no aggregate fallback")
        click.echo("Free = schedulable full nodes when CPU/memory thresholds are known")
        click.echo("GPU Free = READY nodes whose GPUs are fully available")
        click.echo("Idle GPUs = currently idle GPUs; low-priority usage is not included")
        click.echo("LowPri GPUs = advisory current low-priority card count; n/a if unavailable")
        click.echo(
            "LowPri Nodes = estimated nodes from observed low-priority GPU usage; "
            "reclaimability is not guaranteed"
        )
        click.echo("")

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
