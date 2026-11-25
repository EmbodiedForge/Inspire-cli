"""Deprecated compatibility view of the shared Job resource planner."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import resource_plan
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.config import Config, ConfigError
from inspire.config.workspaces import WORKSPACE_ALIAS_HELP, select_workspace_id


def _number(value: Any, convert: type[int] | type[float]) -> int | float:
    try:
        return convert(value or 0)
    except (TypeError, ValueError):
        return convert(0)


def _offer_row(planned: resource_plan.PlannedOffer) -> dict[str, Any]:
    offer = planned.offer
    return {
        "group": offer.logic_compute_group_name,
        "logic_compute_group_id": offer.logic_compute_group_id,
        "quota_id": offer.quota_id,
        "gpu_type": offer.gpu_type,
        "gpu": offer.gpu_count,
        "cpu": offer.cpu_count,
        "memory_gib": offer.memory_size_gib,
        "quota": f"{offer.gpu_count},{offer.cpu_count},{offer.memory_size_gib}",
        "gpu_price": _number(offer.raw.get("gpu_price"), float),
        "price_per_hour": _number(offer.raw.get("total_price_per_hour"), float),
        "raw": offer.raw,
        "allowed_priority_levels": list(planned.priority_evidence.levels),
        "priority_evidence": asdict(planned.priority_evidence),
        "selector": planned.selector,
    }


def list_job_offers(
    *, workspace_id: str, group: str | None, config: Config | None = None
) -> list[dict[str, Any]]:
    plan = resource_plan.build_job_resource_plan(
        workspace_id=workspace_id, group=group, config=config
    )
    return [_offer_row(planned) for planned in plan.offers]


@click.command("quota")
@click.option("--workspace", help=WORKSPACE_ALIAS_HELP + " Required unless --workspace-id is used.")
@click.option("--workspace-id", help="Explicit workspace ID override")
@click.option("--group", help="Filter by compute-group name or ID")
@json_option
@pass_context
def quota(
    ctx: Context,
    workspace: str | None,
    workspace_id: str | None,
    group: str | None,
    json_output: bool = False,
) -> None:
    """List live Scheduling Offers using the same planner as job offers."""
    resolve_json_output(ctx, json_output)
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        resolved_workspace_id = select_workspace_id(
            config,
            explicit_workspace_id=workspace_id,
            explicit_workspace_name=None if workspace_id else workspace,
        )
        if not resolved_workspace_id:
            raise ConfigError("Pass --workspace or --workspace-id; no workspace is assumed")
        plan = resource_plan.build_job_resource_plan(
            workspace_id=resolved_workspace_id,
            group=group,
            config=config,
        )
        offers = [_offer_row(planned) for planned in plan.offers]
        payload = {**plan.as_dict(include_offers=False), "offers": offers}
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
            return
        if not offers:
            click.echo("No Job scheduling offers found.")
        else:
            click.echo("GROUP\tGPU TYPE\tQUOTA (GPU,CPU,MEM)\tPRICE (card/hr·row/hr)\tPRIORITY")
            for offer in offers:
                evidence = offer["priority_evidence"]
                priority = ",".join(evidence["levels"]) or evidence["status"]
                price_cell = (
                    f"{offer['gpu_price']:.2f}·{offer['price_per_hour']:.2f}"
                    if offer["price_per_hour"]
                    else "-"
                )
                click.echo(
                    f"{offer['group']}\t{offer['gpu_type']}\t{offer['quota']}\t"
                    f"{price_cell}\t{priority}\t{offer['selector']}"
                )
        for warning in plan.warnings:
            click.echo(f"Warning: {warning}")
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)


__all__ = ["list_job_offers", "quota"]
