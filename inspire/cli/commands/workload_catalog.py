"""Shared command rendering for workload-specific offers and availability."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Callable, Sequence

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import resource_plan
from inspire.cli.utils.errors import exit_with_error
from inspire.config import Config, ConfigError
from inspire.config.workspaces import WORKSPACE_ALIAS_HELP, select_workspace_id
from inspire.platform.web.browser_api.workload_contracts import (
    WorkloadAvailability,
    WorkloadOfferDiscovery,
    get_workload_availability,
)

OfferDiscoverer = Callable[..., WorkloadOfferDiscovery]

OFFERS_PLACEMENT_CAVEAT = (
    "Offers describe requestable quota/price shapes, not proof of placement; "
    "per-node shapes were not consulted and scheduling is unproven."
)
NODE_PLACEMENT_CAVEAT = (
    "Node-level group totals; per-request CPU/memory fit and scheduler admission are unproven."
)
UNAVAILABLE_PLACEMENT_CAVEAT = "Node-level capacity is unavailable; no aggregate fallback is used, so no capacity claim is made."


def _resolve_workspace(*, workspace: str | None, workspace_id: str | None) -> tuple[Config, str]:
    config, _ = Config.from_files_and_env(require_target_dir=False)
    if not workspace and not workspace_id:
        raise ConfigError("Pass --workspace or --workspace-id; no workspace is assumed")
    resolved = select_workspace_id(
        config,
        explicit_workspace_name=workspace,
        explicit_workspace_id=workspace_id,
    )
    if not resolved:
        raise ConfigError("No workspace selected")
    return config, resolved


def _filter_discovery(
    discovery: WorkloadOfferDiscovery, group: str | None
) -> WorkloadOfferDiscovery:
    needle = str(group or "").strip().lower()
    if not needle:
        return discovery

    def matches(group_id: str, group_name: str) -> bool:
        return needle == group_id.lower() or needle in group_name.lower()

    def matching_item(item: dict) -> bool:
        return matches(
            str(item.get("logic_compute_group_id") or ""),
            str(item.get("logic_compute_group_name") or ""),
        )

    return replace(
        discovery,
        offers=[
            offer
            for offer in discovery.offers
            if matches(offer.logic_compute_group_id, offer.logic_compute_group_name)
        ],
        capability_groups_without_quota_rows=[
            item for item in discovery.capability_groups_without_quota_rows if matching_item(item)
        ],
        quota_rows_without_capability=[
            item for item in discovery.quota_rows_without_capability if matching_item(item)
        ],
        failures=[item for item in discovery.failures if matching_item(item)],
    )


def _workspace_scope_line(workspace: str | None, resolved_workspace: str) -> str:
    alias = f" (--workspace {workspace})" if workspace else ""
    return f"Workspace: {resolved_workspace}{alias}"


def _job_plan_options(command: click.Command, kind: str) -> click.Command:
    if kind != "job":
        return command
    command = click.option("--resource", help="Filter by GPU request, for example 1xH200")(command)
    command = click.option("--quota", help="Filter by exact GPU,CPU,MEM quota shape")(command)
    command = click.option(
        "--priority", type=click.IntRange(1, 10), help="Resolve priority for each matching offer"
    )(command)
    return command


def _print_discovery_warnings(discovery: WorkloadOfferDiscovery) -> None:
    for item in discovery.capability_groups_without_quota_rows:
        click.echo(
            "Warning: workload capability without quota rows: "
            f"{item['logic_compute_group_name']} ({item['logic_compute_group_id']})"
        )
    for item in discovery.quota_rows_without_capability:
        click.echo(
            "Warning: quota rows without workload capability: "
            f"{item['logic_compute_group_name']} ({item['row_count']} rows)"
        )


def build_offers_command(kind: str, discoverer: OfferDiscoverer) -> click.Command:
    @click.command("offers")
    @click.option(
        "--workspace",
        help=WORKSPACE_ALIAS_HELP + " Required unless --workspace-id is used.",
    )
    @click.option("--workspace-id", help="Explicit workspace ID")
    @click.option("--group", help="Filter by exact ID or partial group name")
    @pass_context
    def offers(
        ctx: Context,
        workspace: str | None,
        workspace_id: str | None,
        group: str | None,
        resource: str | None = None,
        quota: str | None = None,
        priority: int | None = None,
    ) -> None:
        """Show live workload-capable group/quota combinations in one explicit workspace."""
        try:
            _config, resolved_workspace = _resolve_workspace(
                workspace=workspace, workspace_id=workspace_id
            )
            plan = None
            if kind == "job":
                plan = resource_plan.build_job_resource_plan(
                    workspace_id=resolved_workspace,
                    config=_config,
                    group=group,
                    resource=resource,
                    quota=quota,
                    priority=priority,
                    priority_from_caller=priority is not None,
                )
                discovery = plan.discovery
                payload = plan.as_dict()
            else:
                discovery = _filter_discovery(discoverer(workspace_id=resolved_workspace), group)
                payload = asdict(discovery)
            payload.update(
                placement_basis="quota-price-offers",
                node_shape_evidence="not_consulted",
                placement_caveat=OFFERS_PLACEMENT_CAVEAT,
            )
            if ctx.json_output:
                click.echo(json_formatter.format_json(payload))
                return
            click.echo(_workspace_scope_line(workspace, resolved_workspace))
            click.echo(f"Placement: unproven; {OFFERS_PLACEMENT_CAVEAT}")
            if not discovery.offers:
                click.echo(f"No {kind} Scheduling Offers found.")
            if plan is not None:
                for planned in plan.offers:
                    offer = planned.offer
                    evidence = planned.priority_evidence
                    levels = ",".join(evidence.levels) if evidence.levels else evidence.status
                    click.echo(
                        f"{offer.logic_compute_group_name}\t{offer.gpu_type or '-'}\t"
                        f"GPU={offer.gpu_count},CPU={offer.cpu_count},MEM={offer.memory_size_gib}\t"
                        f"quota={offer.quota_id}\tpriority={levels}\tselector={planned.selector}"
                    )
            else:
                for offer in discovery.offers:
                    click.echo(
                        f"{offer.logic_compute_group_name}\t{offer.gpu_type or '-'}\t"
                        f"GPU={offer.gpu_count},CPU={offer.cpu_count},MEM={offer.memory_size_gib}\t"
                        f"quota={offer.quota_id}\t"
                        f"priority={','.join(offer.allowed_priority_levels) or 'any'}"
                    )
            _print_discovery_warnings(discovery)
            if plan is not None:
                for warning in plan.warnings:
                    click.echo(f"Warning: {warning}")
        except ConfigError as exc:
            exit_with_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
        except Exception as exc:  # noqa: BLE001
            exit_with_error(ctx, "APIError", str(exc), EXIT_API_ERROR)

    return _job_plan_options(offers, kind)


def build_availability_command(kind: str, discoverer: OfferDiscoverer) -> click.Command:
    @click.command("availability")
    @click.option(
        "--workspace",
        help=WORKSPACE_ALIAS_HELP + " Required unless --workspace-id is used.",
    )
    @click.option("--workspace-id", help="Explicit workspace ID")
    @click.option("--group", help="Filter by exact ID or partial group name")
    @pass_context
    def availability(
        ctx: Context,
        workspace: str | None,
        workspace_id: str | None,
        group: str | None,
        resource: str | None = None,
        quota: str | None = None,
        priority: int | None = None,
    ) -> None:
        """Show advisory capacity and, for Jobs, per-offer priority evidence."""
        try:
            _config, resolved_workspace = _resolve_workspace(
                workspace=workspace, workspace_id=workspace_id
            )
            plan = None
            if kind == "job":
                plan = resource_plan.build_job_resource_plan(
                    workspace_id=resolved_workspace,
                    config=_config,
                    group=group,
                    resource=resource,
                    quota=quota,
                    priority=priority,
                    priority_from_caller=priority is not None,
                    include_capacity=True,
                )
                discovery = plan.discovery
                signals: Sequence[WorkloadAvailability] = plan.availability
                payload = plan.as_dict()
            else:
                discovery = _filter_discovery(discoverer(workspace_id=resolved_workspace), group)
                signals = get_workload_availability(discovery)
                payload = {"workload": kind, "workspace_id": resolved_workspace}
            sources = {item.source for item in signals}
            if signals and sources == {"node_dimensions"}:
                availability_source = "node_dimensions"
                placement_basis = "node-gpu-count"
                placement_caveat = NODE_PLACEMENT_CAVEAT
                fallback_reason = ""
            elif signals and sources == {"unavailable"}:
                availability_source = "unavailable"
                placement_basis = "unavailable"
                placement_caveat = UNAVAILABLE_PLACEMENT_CAVEAT
                fallback_reason = next(
                    (item.error for item in signals if item.error),
                    "node-level capacity unavailable",
                )
            else:
                availability_source = "mixed"
                placement_basis = "node-gpu-count"
                placement_caveat = NODE_PLACEMENT_CAVEAT
                fallback_reason = (
                    "; ".join(item.error for item in signals if item.error)
                    or "some groups have no node-level capacity"
                )
            payload.update(
                advisory=True,
                availability_source=availability_source,
                placement_basis=placement_basis,
                placement_caveat=placement_caveat,
                fallback_reason=fallback_reason,
                signals=[asdict(item) for item in signals],
            )
            if ctx.json_output:
                click.echo(json_formatter.format_json(payload))
                return
            click.echo(_workspace_scope_line(workspace, resolved_workspace))
            click.echo(f"Placement: {placement_basis}; {placement_caveat}")
            if not signals:
                click.echo(f"No {kind} availability signals found.")
            for item in signals:
                if item.error:
                    click.echo(f"{item.logic_compute_group_name}\tunavailable\t{item.error}")
                    continue
                low_priority_gpus = (
                    item.gpu_low_priority_used if item.gpu_low_priority_used is not None else "n/a"
                )
                click.echo(
                    f"{item.logic_compute_group_name}\t"
                    f"CPU={item.cpu_available}/{item.cpu_total}\t"
                    f"GPU={item.gpu_available}/{item.gpu_total}\t"
                    f"LowPriGPU={low_priority_gpus}\t"
                    f"MEM={item.memory_gib_available}/{item.memory_gib_total} GiB\t"
                    f"offers={item.launchable_offer_count}"
                )
            if plan is not None:
                for planned in plan.offers:
                    click.echo(
                        f"Offer: {planned.selector}\tpriority={planned.effective_priority}\t"
                        f"preemptible_usable={planned.as_dict()['preemptible_usable']}"
                    )
                for warning in plan.warnings:
                    click.echo(f"Warning: {warning}")
            _print_discovery_warnings(discovery)
        except ConfigError as exc:
            exit_with_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
        except Exception as exc:  # noqa: BLE001
            exit_with_error(ctx, "APIError", str(exc), EXIT_API_ERROR)

    return _job_plan_options(availability, kind)


__all__ = ["build_availability_command", "build_offers_command"]
