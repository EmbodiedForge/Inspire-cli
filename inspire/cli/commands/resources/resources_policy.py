"""Read-only workspace scheduling policy command."""

from __future__ import annotations

from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web.browser_api.schedule_config import (
    WorkloadSchedulePolicy,
    format_duration,
    get_workspace_schedule_policy,
)
from inspire.platform.web.session import (
    SessionAuthenticationError,
    SessionExpiredError,
    WebSession,
    get_web_session,
)

_WORKLOADS = ("notebook", "job", "hpc", "ray", "serving")
_SERVING_SCOPE_NOTE = "serving: the reclaim rule applies to custom deployments only."


def _workspace_names(config: Config, session: WebSession) -> dict[str, str]:
    names = {
        str(workspace_id): str(name)
        for workspace_id, name in (config.workspace_names or {}).items()
    }
    names.update(
        {
            str(workspace_id): str(name)
            for workspace_id, name in (session.all_workspace_names or {}).items()
        }
    )
    return names


def _resolve_workspace(
    config: Config,
    session: WebSession,
    *,
    workspace: Optional[str],
    workspace_id: Optional[str],
) -> str:
    if workspace_id:
        resolved = select_workspace_id(config, explicit_workspace_id=workspace_id, session=session)
        if resolved:
            return resolved
        raise ConfigError("No workspace selected")

    alias_error: ConfigError | None = None
    try:
        resolved = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            session=session,
        )
        if resolved:
            return resolved
    except ConfigError as exc:
        alias_error = exc

    matches = sorted(
        candidate_id
        for candidate_id, name in _workspace_names(config, session).items()
        if name.strip() == str(workspace or "").strip()
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigError(
            f"Workspace name {workspace!r} is ambiguous; use --workspace-id with one of: "
            + ", ".join(matches)
        )
    if alias_error is not None:
        raise alias_error
    raise ConfigError(f"Unknown workspace name: {workspace!r}")


def _public_row(policy: WorkloadSchedulePolicy) -> dict[str, Any]:
    row: dict[str, Any] = {
        "workload": policy.workload,
        "configured": policy.configured,
    }
    if not policy.configured:
        return row

    row["auto_reclaim"] = bool(policy.auto_reclaim)
    if policy.reclaim_description:
        row["reclaim_rule"] = policy.reclaim_description
    if policy.max_runtime_minutes:
        row["max_runtime_minutes"] = policy.max_runtime_minutes
        row["max_runtime"] = format_duration(policy.max_runtime_minutes)
    if policy.daily_shutdown:
        row["daily_shutdown"] = policy.daily_shutdown
    if policy.applies_to:
        row["applies_to"] = policy.applies_to
    if policy.auto_save is not None:
        row["auto_save"] = policy.auto_save
    return row


def _reclaim(row: dict[str, Any]) -> str:
    if not row.get("configured"):
        return "-"
    return "on" if row.get("auto_reclaim") else "off"


def _time_limit(row: dict[str, Any]) -> str:
    if not row.get("configured"):
        return "-"
    limits = []
    if row.get("max_runtime"):
        limits.append(f"max {row['max_runtime']}")
    if row.get("daily_shutdown"):
        limits.append(f"daily {row['daily_shutdown']}")
    return ", ".join(limits) if limits else "none"


def _notes(rows: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    unconfigured = sorted({str(row["workload"]) for row in rows if not row.get("configured")})
    if unconfigured:
        notes.append(
            f"{', '.join(unconfigured)}: this workspace declares no scheduling policy; "
            "this does not guarantee unlimited runtime."
        )
    if any(row.get("workload") == "serving" and row.get("auto_reclaim") for row in rows):
        notes.append(_SERVING_SCOPE_NOTE)
    return notes


@click.command("policy")
@click.option("--workspace", metavar="NAME", help="Workspace alias or exact display name")
@click.option("--workspace-id", help="Explicit workspace ID")
@click.option(
    "--workload",
    type=click.Choice(_WORKLOADS, case_sensitive=False),
    help="Show one workload only (default: all five)",
)
@pass_context
def policy_resources(
    ctx: Context,
    workspace: Optional[str],
    workspace_id: Optional[str],
    workload: Optional[str],
) -> None:
    """Show idle-reclaim rules and runtime limits for each workload.

    A dash means the workspace declares no policy for that workload, not that
    the workload is guaranteed unlimited runtime.

    \b
    Examples:
        inspire resources policy --workspace gpu
        inspire resources policy --workspace 分布式训练空间 --workload notebook
        inspire --json resources policy --workspace-id ws-xxx
    """
    if bool(workspace) == bool(workspace_id):
        _handle_error(
            ctx,
            "ValidationError",
            "Select exactly one workspace with --workspace or --workspace-id.",
            EXIT_VALIDATION_ERROR,
        )
        return

    try:
        config, _ = Config.from_files_and_env(
            require_credentials=False,
            require_target_dir=False,
        )
        session = get_web_session()
        resolved_workspace_id = _resolve_workspace(
            config,
            session,
            workspace=workspace,
            workspace_id=workspace_id,
        )
        workspace_label = _workspace_names(config, session).get(
            resolved_workspace_id,
            workspace or resolved_workspace_id,
        )
        selected = workload.lower() if workload else None
        rows = [
            _public_row(policy)
            for policy in get_workspace_schedule_policy(resolved_workspace_id, session=session)
            if not selected or policy.workload == selected
        ]
        payload = {
            "workspace_id": resolved_workspace_id,
            "workspace": workspace_label,
            "items": rows,
            "total": len(rows),
        }
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
            return

        click.echo(f"Workspace: {workspace_label} ({resolved_workspace_id})")
        click.echo("Workload\tReclaim\tIdle Rule\tTime Limit")
        for row in rows:
            click.echo(
                f"{row['workload']}\t{_reclaim(row)}\t"
                f"{row.get('reclaim_rule') or ('-' if not row.get('configured') else 'none')}\t"
                f"{_time_limit(row)}"
            )
        for note in _notes(rows):
            click.echo(note)
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except (SessionAuthenticationError, SessionExpiredError) as exc:
        _handle_error(ctx, "AuthenticationError", str(exc), EXIT_AUTH_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)


__all__ = ["policy_resources"]
