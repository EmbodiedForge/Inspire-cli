"""Ray workload commands."""

from __future__ import annotations

from dataclasses import asdict
import json
import time
from typing import Any, Callable

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.resource_metrics_cli import (
    metrics_payload,
    metrics_text_lines,
    parse_metric_types,
)
from inspire.cli.utils.workload_launch import prepare_ray_launch, submit_prepared_launch
from inspire.cli.commands.workload_profile import build_profile_group
from inspire.cli.commands.workload_catalog import build_availability_command
from inspire.cli.commands.workload_batch import build_batch_command
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api


def _workspace_id(workspace: str | None, workspace_id: str | None) -> str:
    config, _ = Config.from_files_and_env(require_target_dir=False)
    resolved = (
        select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            explicit_workspace_id=workspace_id,
        )
        or config.default_workspace_id
    )
    if not resolved:
        raise ConfigError(
            "No workspace selected for Ray. Pass --workspace/--workspace-id or set "
            "[defaults].workspace_id."
        )
    return resolved


def _emit(ctx: Context, payload: dict[str, Any], render: Callable[[], None]) -> None:
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        render()


def _run(ctx: Context, operation: Callable[[], None]) -> None:
    try:
        operation()
    except ConfigError as exc:
        exit_with_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        exit_with_error(ctx, "APIError", str(exc), EXIT_API_ERROR)


@click.group()
def ray() -> None:
    """Create and operate elastic Ray jobs."""


ray.add_command(build_profile_group("ray"))
ray.add_command(build_batch_command("ray"))
ray.add_command(
    build_availability_command("ray", lambda **kwargs: browser_api.list_ray_offers(**kwargs))
)


def _parse_workers(values: tuple[str, ...]) -> list[dict[str, Any]] | None:
    if not values:
        return None
    workers: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        try:
            item = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"--worker {index} is not valid JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ConfigError(f"--worker {index} must be a JSON object")
        workers.append(item)
    return workers


@ray.command("create")
@click.option("--name", "name", "-n", required=True, help="Ray job name")
@click.option("--entrypoint", "entrypoint", "--command", "-c", required=True)
@click.option("--profile", "profile_name", help="Portable Ray workload profile")
@click.option("--workspace", help="Exact account-scoped workspace alias")
@click.option("--workspace-id", help="Exact workspace ID")
@click.option("--project", help="Exact project ID, configured alias, or unique live name")
@click.option("--group", help="Exact head compute-group ID or name")
@click.option("--quota", help="Exact head GPU,CPU,MEM Scheduling Offer")
@click.option("--image", help="Exact launchable head image ID, URL, name, or name:version")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    help=(
        "Platform task priority 1-10 "
        "(values above the project's own priority are capped automatically)"
    ),
)
@click.option("--description", help="Ray job description")
@click.option("--head-shm-size", type=click.IntRange(1), help="Head shared memory in GiB")
@click.option("--worker-name", help="Name for the default worker group")
@click.option("--min-replicas", type=click.IntRange(1), help="Default worker minimum")
@click.option("--max-replicas", type=click.IntRange(1), help="Default worker maximum")
@click.option(
    "--worker",
    "workers_json",
    multiple=True,
    help="Repeatable worker JSON object with name/group/quota/image/min_replicas/max_replicas",
)
@click.option("--dry-run", is_flag=True, help="Resolve live bindings and skip mutation")
@pass_context
def create(
    ctx: Context,
    name: str,
    entrypoint: str,
    profile_name: str | None,
    workspace: str | None,
    workspace_id: str | None,
    project: str | None,
    group: str | None,
    quota: str | None,
    image: str | None,
    priority: int | None,
    description: str | None,
    head_shm_size: int | None,
    worker_name: str | None,
    min_replicas: int | None,
    max_replicas: int | None,
    workers_json: tuple[str, ...],
    dry_run: bool,
) -> None:
    """Prepare and optionally submit a Ray workload launch."""

    def operation() -> None:
        workers = _parse_workers(workers_json)
        if workers is None and any(
            value is not None for value in (worker_name, min_replicas, max_replicas)
        ):
            workers = [
                {
                    "name": worker_name or "workers",
                    "group": group,
                    "quota": quota,
                    "image": image,
                    "min_replicas": min_replicas or 1,
                    "max_replicas": max_replicas or 2,
                }
            ]
        config, _ = Config.from_files_and_env(require_target_dir=False)
        prepared = prepare_ray_launch(
            config=config,
            name=name,
            entrypoint=entrypoint,
            profile_name=profile_name,
            explicit={
                "workspace": workspace,
                "project": project,
                "group": group,
                "quota": quota,
                "image": image,
                "priority": priority,
                "description": description,
                "head": {"shm_size": head_shm_size} if head_shm_size else None,
                "workers": workers,
            },
            explicit_workspace_id=workspace_id,
        )
        result = None if dry_run else submit_prepared_launch(prepared)
        payload = {
            "workload": "ray",
            "dry_run": dry_run,
            "preview": prepared.preview.to_dict(),
            "result": result,
        }

        def render() -> None:
            offers = prepared.preview.scheduling_offer or {}
            head = offers.get("head", {}) if isinstance(offers, dict) else {}
            click.echo(f"Ray launch: {name}")
            click.echo(f"Workspace: {prepared.preview.resolved['workspace']['id']}")
            click.echo(f"Project:   {prepared.preview.resolved['project']['name']}")
            click.echo(
                f"Head:      {head.get('logic_compute_group_name')} "
                f"GPU={head.get('gpu_count')} CPU={head.get('cpu_count')} "
                f"MEM={head.get('memory_size_gib')}"
            )
            click.echo(
                f"Workers:   {len(prepared.preview.platform_request.get('worker_groups', []))}"
            )
            for warning in prepared.preview.warnings:
                click.echo(f"Warning: {warning}")
            click.echo("No Ray job was submitted." if dry_run else "Ray job submitted.")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@ray.command("stop")
@click.argument("ray_job_id")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Stop without an interactive confirmation (alias: --force)",
)
@pass_context
def stop(ctx: Context, ray_job_id: str, yes: bool) -> None:
    """Stop a running Ray job."""
    if not confirmed_or_error(ctx, assume_yes=yes, subject=f"stop Ray job {ray_job_id}"):
        raise click.Abort()

    def operation() -> None:
        result = browser_api.stop_ray_job(ray_job_id)
        _emit(
            ctx,
            {"workload": "ray", "ray_job_id": ray_job_id, "result": result},
            lambda: click.echo(f"Stopped Ray job {ray_job_id}."),
        )

    _run(ctx, operation)


@ray.command("delete")
@click.argument("ray_job_id")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Delete without an interactive confirmation (alias: --force)",
)
@pass_context
def delete(ctx: Context, ray_job_id: str, yes: bool) -> None:
    """Delete a Ray job record."""
    if not confirmed_or_error(ctx, assume_yes=yes, subject=f"delete Ray job {ray_job_id}"):
        raise click.Abort()

    def operation() -> None:
        result = browser_api.delete_ray_job(ray_job_id)
        _emit(
            ctx,
            {"workload": "ray", "ray_job_id": ray_job_id, "result": result},
            lambda: click.echo(f"Deleted Ray job {ray_job_id}."),
        )

    _run(ctx, operation)


@ray.command("list")
@click.option("--workspace", help="CPU workspace alias or ID")
@click.option("--workspace-id", help="Explicit workspace ID")
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
@click.option("--all-users", is_flag=True, help="List jobs from all visible users")
@pass_context
def list_jobs(
    ctx: Context,
    workspace: str | None,
    workspace_id: str | None,
    limit: int,
    all_users: bool,
) -> None:
    """List Ray jobs in one workspace."""

    def operation() -> None:
        resolved_workspace_id = _workspace_id(workspace, workspace_id)
        user_ids = None
        if not all_users:
            current_user = browser_api.get_current_user()
            current_user_id = str(
                current_user.get("id") or current_user.get("user_id") or ""
            ).strip()
            if not current_user_id:
                raise ValueError("Current platform user did not include an id")
            user_ids = [current_user_id]
        jobs, total = browser_api.list_ray_jobs(
            workspace_id=resolved_workspace_id,
            user_ids=user_ids,
            page_size=limit,
        )
        items = [asdict(job) for job in jobs]
        payload = {
            "workload": "ray",
            "workspace_id": resolved_workspace_id,
            "total": total,
            "items": items,
        }

        def render() -> None:
            if not jobs:
                click.echo("No Ray jobs found.")
                return
            for job in jobs:
                click.echo(f"{job.ray_job_id}\t{job.status or '-'}\t{job.name or '-'}")
            if total > len(jobs):
                click.echo(f"Showing {len(jobs)} of {total} jobs.")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@ray.command("status")
@click.argument("ray_job_id")
@pass_context
def status(ctx: Context, ray_job_id: str) -> None:
    """Show the raw platform detail for a Ray job."""

    def operation() -> None:
        detail = browser_api.get_ray_job_detail(ray_job_id)
        payload = {"workload": "ray", "ray_job_id": ray_job_id, "detail": detail}

        def render() -> None:
            click.echo(f"Job:      {ray_job_id}")
            click.echo(f"Name:     {detail.get('name') or detail.get('job_name') or '-'}")
            click.echo(f"Status:   {detail.get('status') or '-'}")
            click.echo(f"Project:  {detail.get('project_name') or detail.get('project_id') or '-'}")
            workers = detail.get("worker_groups")
            if isinstance(workers, list):
                click.echo(f"Workers:  {len(workers)} group(s)")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@ray.command("events")
@click.argument("ray_job_id")
@pass_context
def events(ctx: Context, ray_job_id: str) -> None:
    """List scheduler events for a Ray job."""

    def operation() -> None:
        items = browser_api.list_ray_job_events(ray_job_id)
        payload = {"workload": "ray", "ray_job_id": ray_job_id, "events": items}

        def render() -> None:
            if not items:
                click.echo("No Ray events found.")
                return
            for item in items:
                timestamp = item.get("last_timestamp") or item.get("event_timestamp") or "-"
                click.echo(f"{timestamp}\t{item.get('reason') or '-'}\t{item.get('message') or ''}")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@ray.command("instances")
@click.argument("ray_job_id")
@click.option("--limit", type=click.IntRange(1, 500), default=100, show_default=True)
@pass_context
def instances(ctx: Context, ray_job_id: str, limit: int) -> None:
    """List head and worker instances for a Ray job."""

    def operation() -> None:
        items, total = browser_api.list_ray_job_instances(ray_job_id, page_size=limit)
        payload = {
            "workload": "ray",
            "ray_job_id": ray_job_id,
            "total": total,
            "items": items,
        }

        def render() -> None:
            if not items:
                click.echo("No Ray instances found.")
                return
            for item in items:
                click.echo(
                    f"{item.get('instance_id') or item.get('name') or '-'}\t"
                    f"{item.get('instance_type') or '-'}\t"
                    f"{item.get('worker_group_name') or '-'}\t{item.get('status') or '-'}"
                )

        _emit(ctx, payload, render)

    _run(ctx, operation)


@ray.command("logs")
@click.argument("ray_job_id")
@click.option("--since", type=click.IntRange(1), default=3600, show_default=True)
@click.option("--limit", type=click.IntRange(1, 1000), default=200, show_default=True)
@pass_context
def logs(ctx: Context, ray_job_id: str, since: int, limit: int) -> None:
    """Fetch aggregated platform logs for current Ray head and worker pods."""

    def operation() -> None:
        instances = browser_api.list_ray_job_log_instances(ray_job_id)
        pod_names: list[str] = []
        for item in instances:
            name = str(
                item.get("name")
                or item.get("pod_name")
                or item.get("podName")
                or item.get("instance_name")
                or item.get("instance_id")
                or ""
            ).strip()
            if name and name not in pod_names:
                pod_names.append(name)
        if not pod_names:
            raise ValueError(f"Ray job {ray_job_id} has no instance pod names for log lookup")
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - since * 1000
        items, total = browser_api.list_ray_job_logs(
            pod_names=pod_names,
            start_timestamp_ms=start_ms,
            end_timestamp_ms=end_ms,
            page_size=limit,
            ray_job_id=ray_job_id,
        )
        payload = {
            "workload": "ray",
            "ray_job_id": ray_job_id,
            "pod_names": pod_names,
            "total": total,
            "items": items,
        }

        def render() -> None:
            if not items:
                click.echo("No Ray logs found.")
                return
            for item in items:
                timestamp = (
                    item.get("time") or item.get("@timestamp") or item.get("timestamp") or "-"
                )
                pod = item.get("pod_name") or item.get("podName") or item.get("pod") or "-"
                message = item.get("message") or item.get("log") or item.get("content") or item
                click.echo(f"{timestamp}\t{pod}\t{message}")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@ray.command("scaling-histories")
@click.argument("ray_job_id")
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
@pass_context
def scaling_histories(ctx: Context, ray_job_id: str, limit: int) -> None:
    """List elastic worker-group scaling history for a Ray job."""

    def operation() -> None:
        items, total = browser_api.list_ray_job_scaling_histories(ray_job_id, page_size=limit)
        payload = {
            "workload": "ray",
            "ray_job_id": ray_job_id,
            "total": total,
            "items": items,
        }

        def render() -> None:
            if not items:
                click.echo("No Ray scaling history found.")
                return
            for item in items:
                click.echo(
                    f"{item.get('created_at') or item.get('time') or '-'}\t"
                    f"{item.get('worker_group_name') or item.get('group_name') or '-'}\t"
                    f"{item.get('message') or item.get('reason') or item}"
                )

        _emit(ctx, payload, render)

    _run(ctx, operation)


@ray.command("metrics")
@click.argument("ray_job_id")
@click.option("--metric", "metric_types", multiple=True, help="Repeatable or comma-separated")
@click.option("--interval", type=click.IntRange(1), default=300, show_default=True)
@click.option("--raw", is_flag=True, help="Return platform-shaped metric payloads")
@click.option("--series", is_flag=True, help="Include full time series")
@pass_context
def metrics(
    ctx: Context,
    ray_job_id: str,
    metric_types: tuple[str, ...],
    interval: int,
    raw: bool,
    series: bool,
) -> None:
    """Fetch platform historical resource metrics for a Ray job."""

    def operation() -> None:
        result = browser_api.get_ray_job_resource_metrics(
            ray_job_id,
            metric_types=parse_metric_types(metric_types),
            interval_seconds=interval,
            raw=raw,
        )
        if ctx.json_output or raw:
            click.echo(
                json_formatter.format_json(
                    metrics_payload(
                        result,
                        id_key="ray_job_id",
                        raw=raw,
                        include_series=series,
                    )
                )
            )
            return
        for line in metrics_text_lines(result, label="Ray job", include_series=series):
            click.echo(line)

    _run(ctx, operation)


@ray.command("offers")
@click.option("--workspace", help="CPU workspace alias or ID")
@click.option("--workspace-id", help="Explicit workspace ID")
@pass_context
def offers(ctx: Context, workspace: str | None, workspace_id: str | None) -> None:
    """Show live launchable Ray group/quota combinations."""

    def operation() -> None:
        resolved_workspace_id = _workspace_id(workspace, workspace_id)
        discovery = browser_api.list_ray_offers(workspace_id=resolved_workspace_id)
        payload = asdict(discovery)

        def render() -> None:
            click.echo(f"Ray offers: {len(discovery.offers)}")
            for offer in discovery.offers:
                click.echo(
                    f"{offer.logic_compute_group_name}\t{offer.cpu_count} CPU\t"
                    f"{offer.memory_size_gib} GiB\tquota={offer.quota_id}"
                )
            for signal in discovery.capability_groups_without_quota_rows:
                click.echo(
                    "Warning: capability without quota: "
                    f"{signal['logic_compute_group_name']} ({signal['logic_compute_group_id']})"
                )
            for signal in discovery.quota_rows_without_capability:
                click.echo(
                    "Warning: quota without capability: "
                    f"{signal['logic_compute_group_name']} ({signal['row_count']} rows)"
                )

        _emit(ctx, payload, render)

    _run(ctx, operation)


__all__ = ["ray"]
