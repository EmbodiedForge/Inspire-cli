"""HPC workload commands."""

from __future__ import annotations

from dataclasses import asdict
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
from inspire.cli.utils.workload_launch import prepare_hpc_launch, submit_prepared_launch
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
            "No workspace selected for HPC. Pass --workspace/--workspace-id or set "
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
def hpc() -> None:
    """Create and operate high-performance computing jobs."""


hpc.add_command(build_profile_group("hpc"))
hpc.add_command(build_batch_command("hpc"))
hpc.add_command(
    build_availability_command("hpc", lambda **kwargs: browser_api.list_hpc_offers(**kwargs))
)


@hpc.command("create")
@click.option("--name", "name", "-n", required=True, help="HPC job name")
@click.option(
    "--entrypoint",
    "entrypoint",
    "--command",
    "-c",
    required=True,
    help="Slurm script body to execute; do not include #SBATCH headers",
)
@click.option("--profile", "profile_name", help="Portable HPC workload profile")
@click.option("--workspace", help="Exact account-scoped workspace alias")
@click.option("--workspace-id", help="Exact workspace ID")
@click.option("--project", help="Exact project ID, configured alias, or unique live name")
@click.option("--group", help="Exact live compute-group ID or name")
@click.option("--quota", help="Exact GPU,CPU,MEM Scheduling Offer")
@click.option("--image", help="Exact launchable image ID, URL, name, or name:version")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    help=(
        "Platform task priority 1-10 "
        "(values above the project's own priority are capped automatically)"
    ),
)
@click.option("--instance-count", type=click.IntRange(1), help="Slurm node count")
@click.option("--number-of-tasks", type=click.IntRange(1), help="Slurm task count")
@click.option("--cpus-per-task", type=click.IntRange(1), help="CPUs assigned to each task")
@click.option("--memory-per-cpu", type=click.IntRange(1), help="GiB assigned per task CPU")
@click.option("--memory-per-node", type=click.IntRange(1), help="GiB assigned per node")
@click.option("--job-max-time", help="Platform Slurm maximum-time value")
@click.option(
    "--enable-hyper-threading/--disable-hyper-threading",
    default=None,
    help="Set the native Slurm hyper-threading flag",
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
    instance_count: int | None,
    number_of_tasks: int | None,
    cpus_per_task: int | None,
    memory_per_cpu: int | None,
    memory_per_node: int | None,
    job_max_time: str | None,
    enable_hyper_threading: bool | None,
    dry_run: bool,
) -> None:
    """Prepare and optionally submit an HPC workload launch."""

    def operation() -> None:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        prepared = prepare_hpc_launch(
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
                "instance_count": instance_count,
                "number_of_tasks": number_of_tasks,
                "cpus_per_task": cpus_per_task,
                "memory_per_cpu": memory_per_cpu,
                "memory_per_node": memory_per_node,
                "job_max_time": job_max_time,
                "enable_hyper_threading": enable_hyper_threading,
            },
            explicit_workspace_id=workspace_id,
        )
        result = None if dry_run else submit_prepared_launch(prepared)
        payload = {
            "workload": "hpc",
            "dry_run": dry_run,
            "preview": prepared.preview.to_dict(),
            "result": result,
        }

        def render() -> None:
            scheduling_offer = prepared.preview.scheduling_offer
            offer = scheduling_offer if isinstance(scheduling_offer, dict) else {}
            click.echo(f"HPC launch: {name}")
            click.echo(f"Workspace: {prepared.preview.resolved['workspace']['id']}")
            click.echo(f"Project:   {prepared.preview.resolved['project']['name']}")
            click.echo(
                f"Offer:     {offer.get('logic_compute_group_name')} "
                f"GPU={offer.get('gpu_count')} CPU={offer.get('cpu_count')} "
                f"MEM={offer.get('memory_size_gib')}"
            )
            for warning in prepared.preview.warnings:
                click.echo(f"Warning: {warning}")
            click.echo("No HPC job was submitted." if dry_run else "HPC job submitted.")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@hpc.command("stop")
@click.argument("job_id")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Stop without an interactive confirmation (alias: --force)",
)
@pass_context
def stop(ctx: Context, job_id: str, yes: bool) -> None:
    """Stop a running HPC job."""
    if not confirmed_or_error(ctx, assume_yes=yes, subject=f"stop HPC job {job_id}"):
        raise click.Abort()

    def operation() -> None:
        result = browser_api.stop_hpc_job(job_id)
        _emit(
            ctx,
            {"workload": "hpc", "job_id": job_id, "result": result},
            lambda: click.echo(f"Stopped HPC job {job_id}."),
        )

    _run(ctx, operation)


@hpc.command("delete")
@click.argument("job_id")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Delete without an interactive confirmation (alias: --force)",
)
@pass_context
def delete(ctx: Context, job_id: str, yes: bool) -> None:
    """Delete an HPC job record."""
    if not confirmed_or_error(ctx, assume_yes=yes, subject=f"delete HPC job {job_id}"):
        raise click.Abort()

    def operation() -> None:
        result = browser_api.delete_hpc_job(job_id)
        _emit(
            ctx,
            {"workload": "hpc", "job_id": job_id, "result": result},
            lambda: click.echo(f"Deleted HPC job {job_id}."),
        )

    _run(ctx, operation)


@hpc.command("list")
@click.option("--workspace", help="CPU workspace alias or ID")
@click.option("--workspace-id", help="Explicit workspace ID")
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
@click.option("--status", help="Platform status filter")
@click.option("--all-users", is_flag=True, help="List jobs from all visible users")
@pass_context
def list_jobs(
    ctx: Context,
    workspace: str | None,
    workspace_id: str | None,
    limit: int,
    status: str | None,
    all_users: bool,
) -> None:
    """List HPC jobs in one workspace."""

    def operation() -> None:
        resolved_workspace_id = _workspace_id(workspace, workspace_id)
        created_by = None
        if not all_users:
            current_user = browser_api.get_current_user()
            created_by = str(current_user.get("id") or current_user.get("user_id") or "").strip()
            if not created_by:
                raise ValueError("Current platform user did not include an id")
        jobs, total = browser_api.list_hpc_jobs(
            workspace_id=resolved_workspace_id,
            created_by=created_by,
            status=status,
            page_size=limit,
        )
        items = [asdict(job) for job in jobs]
        payload = {
            "workload": "hpc",
            "workspace_id": resolved_workspace_id,
            "total": total,
            "items": items,
        }

        def render() -> None:
            if not jobs:
                click.echo("No HPC jobs found.")
                return
            for job in jobs:
                click.echo(f"{job.job_id}\t{job.status or '-'}\t{job.name or '-'}")
            if total > len(jobs):
                click.echo(f"Showing {len(jobs)} of {total} jobs.")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@hpc.command("status")
@click.argument("job_id")
@pass_context
def status(ctx: Context, job_id: str) -> None:
    """Show the raw platform detail for an HPC job."""

    def operation() -> None:
        detail = browser_api.get_hpc_job_detail(job_id)
        payload = {"workload": "hpc", "job_id": job_id, "detail": detail}

        def render() -> None:
            click.echo(f"Job:      {job_id}")
            click.echo(f"Name:     {detail.get('name') or detail.get('job_name') or '-'}")
            click.echo(f"Status:   {detail.get('status') or '-'}")
            click.echo(f"Project:  {detail.get('project_name') or detail.get('project_id') or '-'}")
            click.echo(
                "Group:    "
                f"{detail.get('logic_compute_group_name') or detail.get('logic_compute_group_id') or '-'}"
            )

        _emit(ctx, payload, render)

    _run(ctx, operation)


@hpc.command("events")
@click.argument("job_id")
@pass_context
def events(ctx: Context, job_id: str) -> None:
    """List scheduler events for an HPC job."""

    def operation() -> None:
        items = browser_api.list_hpc_job_events(job_id)
        payload = {"workload": "hpc", "job_id": job_id, "events": items}

        def render() -> None:
            if not items:
                click.echo("No HPC events found.")
                return
            for item in items:
                timestamp = item.get("last_timestamp") or item.get("event_timestamp") or "-"
                click.echo(f"{timestamp}\t{item.get('reason') or '-'}\t{item.get('message') or ''}")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@hpc.command("instances")
@click.argument("job_id")
@click.option("--limit", type=click.IntRange(1, 500), default=100, show_default=True)
@pass_context
def instances(ctx: Context, job_id: str, limit: int) -> None:
    """List component/pod instances for an HPC job."""

    def operation() -> None:
        items, total = browser_api.list_hpc_job_instances(job_id, page_size=limit)
        payload = {"workload": "hpc", "job_id": job_id, "total": total, "items": items}

        def render() -> None:
            if not items:
                click.echo("No HPC instances found.")
                return
            for item in items:
                click.echo(
                    f"{item.get('name') or '-'}\t{item.get('component') or '-'}\t"
                    f"{item.get('status') or item.get('instance_status') or '-'}\t"
                    f"{item.get('node') or '-'}"
                )

        _emit(ctx, payload, render)

    _run(ctx, operation)


@hpc.command("logs")
@click.argument("job_id")
@click.option("--since", type=click.IntRange(1), default=3600, show_default=True)
@click.option("--limit", type=click.IntRange(1, 1000), default=200, show_default=True)
@pass_context
def logs(ctx: Context, job_id: str, since: int, limit: int) -> None:
    """Fetch aggregated platform logs for the job's current pod instances."""

    def operation() -> None:
        job_instances, _ = browser_api.list_hpc_job_instances(job_id, page_size=500)
        pod_names = []
        for item in job_instances:
            name = str(
                item.get("name") or item.get("pod_name") or item.get("instance_name") or ""
            ).strip()
            if name and name not in pod_names:
                pod_names.append(name)
        if not pod_names:
            raise ValueError(f"HPC job {job_id} has no instance pod names for log lookup")
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - since * 1000
        items, total = browser_api.list_hpc_job_logs(
            pod_names=pod_names,
            start_timestamp_ms=start_ms,
            end_timestamp_ms=end_ms,
            page_size=limit,
            job_id=job_id,
        )
        payload = {
            "workload": "hpc",
            "job_id": job_id,
            "pod_names": pod_names,
            "total": total,
            "items": items,
        }

        def render() -> None:
            if not items:
                click.echo("No HPC logs found.")
                return
            for item in items:
                timestamp = item.get("@timestamp") or item.get("timestamp") or "-"
                pod = item.get("pod_name") or item.get("podName") or "-"
                message = item.get("message") or item.get("log") or item
                click.echo(f"{timestamp}\t{pod}\t{message}")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@hpc.command("metrics")
@click.argument("job_id")
@click.option("--metric", "metric_types", multiple=True, help="Repeatable or comma-separated")
@click.option("--interval", type=click.IntRange(1), default=300, show_default=True)
@click.option("--raw", is_flag=True, help="Return platform-shaped metric payloads")
@click.option("--series", is_flag=True, help="Include full time series")
@pass_context
def metrics(
    ctx: Context,
    job_id: str,
    metric_types: tuple[str, ...],
    interval: int,
    raw: bool,
    series: bool,
) -> None:
    """Fetch platform historical resource metrics for an HPC job."""

    def operation() -> None:
        result = browser_api.get_hpc_job_resource_metrics(
            job_id,
            metric_types=parse_metric_types(metric_types),
            interval_seconds=interval,
            raw=raw,
        )
        if ctx.json_output or raw:
            click.echo(
                json_formatter.format_json(
                    metrics_payload(
                        result,
                        id_key="job_id",
                        raw=raw,
                        include_series=series,
                    )
                )
            )
            return
        for line in metrics_text_lines(result, label="HPC job", include_series=series):
            click.echo(line)

    _run(ctx, operation)


@hpc.command("offers")
@click.option("--workspace", help="CPU workspace alias or ID")
@click.option("--workspace-id", help="Explicit workspace ID")
@pass_context
def offers(ctx: Context, workspace: str | None, workspace_id: str | None) -> None:
    """Show live launchable HPC group/quota combinations."""

    def operation() -> None:
        resolved_workspace_id = _workspace_id(workspace, workspace_id)
        discovery = browser_api.list_hpc_offers(workspace_id=resolved_workspace_id)
        payload = asdict(discovery)

        def render() -> None:
            click.echo(f"HPC offers: {len(discovery.offers)}")
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


__all__ = ["hpc"]
