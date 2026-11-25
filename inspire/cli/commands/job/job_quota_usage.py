"""Read-only view of live quota consumption, per user or per project."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import job_submit
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.config import Config, ConfigError
from inspire.config.workspaces import WORKSPACE_ALIAS_HELP, select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module

#: The platform's own spelling of "this resource has no cap".
_UNLIMITED = -1


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _limit(value: Any) -> int | None:
    """A platform limit as JSON: ``None`` (null) when the platform reports no cap."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return None if number == _UNLIMITED else number


def _limit_text(value: int | None) -> str:
    return "unlimited" if value is None else str(value)


def _quota_view(quota: dict[str, Any]) -> dict[str, Any]:
    """The platform's quota block, with ``-1`` limits normalised to ``None``."""
    return {
        "gpu_used": _safe_int(quota.get("gpu_used")),
        "gpu_limit": _limit(quota.get("gpu_limit")),
        "cpu_used": _safe_int(quota.get("cpu_used")),
        "cpu_limit": _limit(quota.get("cpu_limit")),
        "memory_gi_used": _safe_int(quota.get("memory_gi_used")),
        "memory_gi_limit": _limit(quota.get("memory_gi_limit")),
    }


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    """One quota-holding workload, flattened out of the platform's nested resource totals."""
    resources = job.get("db_compute_resources")
    if not isinstance(resources, dict):
        resources = {}
    return {
        "job_id": str(job.get("job_id") or ""),
        "name": str(job.get("job_name") or ""),
        "job_type": str(job.get("job_type") or ""),
        "status": str(job.get("status_name") or ""),
        "gpu": _safe_int(resources.get("gpu_total")),
        "cpu": _safe_int(resources.get("cpu_total")),
        "memory_gi": _safe_int(resources.get("memory_gi_total")),
    }


def read_quota_usage(config: Config, *, workspace_id: str, project: str | None) -> dict[str, Any]:
    """One quota-consumption read: the current user's, or one named project's.

    Project names and IDs both resolve through the same path `job create` uses, so a name that is
    ambiguous or invisible in this workspace fails here the same way it would fail at submit time.
    """
    session = web_session_module.get_web_session(account=config.account_alias)
    payload: dict[str, Any] = {"workspace_id": workspace_id}

    if project:
        selected, _fallback = job_submit.select_project_for_workspace(
            config,
            workspace_id=workspace_id,
            requested=project,
        )
        result = browser_api_module.get_project_quota_jobs(
            workspace_id=workspace_id,
            project_id=selected.project_id,
            session=session,
        )
        payload["scope"] = "project"
        payload["project_id"] = selected.project_id
        payload["project_name"] = selected.name
    else:
        current_user = browser_api_module.get_current_user(session=session)
        user_id = str(current_user.get("id") or current_user.get("user_id") or "").strip()
        if not user_id:
            raise ConfigError("Current platform user did not include an id")
        result = browser_api_module.get_user_quota_jobs(
            workspace_id=workspace_id,
            user_id=user_id,
            session=session,
        )
        payload["scope"] = "user"
        payload["user_id"] = user_id

    quota = result.get("quota")
    jobs = result.get("used_quota_jobs")
    payload["quota"] = _quota_view(quota if isinstance(quota, dict) else {})
    payload["jobs"] = [
        _job_view(job) for job in (jobs if isinstance(jobs, list) else []) if isinstance(job, dict)
    ]
    return payload


@click.command("quota-usage")
@click.option("--workspace", help=WORKSPACE_ALIAS_HELP + " Required unless --workspace-id is used.")
@click.option("--workspace-id", help="Explicit workspace ID override")
@click.option("--project", help="Project name or ID: report this project instead of the caller")
@json_option
@pass_context
def quota_usage(
    ctx: Context,
    workspace: str | None,
    workspace_id: str | None,
    project: str | None,
    json_output: bool = False,
) -> None:
    """Show what quota is in use right now, and by which workloads.

    Reads live consumption for one scope: the calling user by default, or one project with
    --project. The job list covers every workload holding the quota - training jobs, notebooks,
    tensorboards. A null limit (text: "unlimited") means the platform sets no cap on that resource.
    """
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
        payload = read_quota_usage(
            config,
            workspace_id=resolved_workspace_id,
            project=project,
        )
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
            return
        quota = payload["quota"]
        click.echo(f"GPU: {quota['gpu_used']} used / {_limit_text(quota['gpu_limit'])}")
        click.echo(f"CPU: {quota['cpu_used']} used / {_limit_text(quota['cpu_limit'])}")
        click.echo(
            f"MEM: {quota['memory_gi_used']} GiB used / {_limit_text(quota['memory_gi_limit'])}"
        )
        if not payload["jobs"]:
            click.echo("No workloads holding quota.")
            return
        click.echo("NAME\tID\tTYPE\tSTATUS\tGPU\tCPU\tMEM")
        for job in payload["jobs"]:
            click.echo(
                f"{job['name']}\t{job['job_id']}\t{job['job_type']}\t{job['status']}"
                f"\t{job['gpu']}\t{job['cpu']}\t{job['memory_gi']}"
            )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except ValueError as exc:
        # select_project_for_workspace raises ValueError for an unknown/ambiguous project —
        # a caller input problem, not a platform failure (same mapping as job create).
        error_type = "QuotaExceeded" if "over quota" in str(exc) else "ValidationError"
        _handle_error(ctx, error_type, str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)


__all__ = ["read_quota_usage", "quota_usage"]
