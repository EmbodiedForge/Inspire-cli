"""Workspace resolution for `inspire notebook create`."""

from __future__ import annotations

from typing import Optional

from inspire.cli.context import Context, EXIT_CONFIG_ERROR
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.platform.web.session import WebSession
from inspire.config.workspaces import select_workspace_id

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"


def resolve_notebook_workspace_id(
    ctx: Context,
    *,
    config: Config,
    session: WebSession,
    workspace: Optional[str],
    workspace_id: Optional[str],
    gpu_count: int,
    gpu_pattern: str,
) -> str | None:
    try:
        auto_workspace_id = select_workspace_id(
            config,
            gpu_type=gpu_pattern if gpu_count > 0 else None,
            cpu_only=(gpu_count == 0),
            explicit_workspace_id=workspace_id,
            explicit_workspace_name=workspace,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return None

    if not auto_workspace_id:
        auto_workspace_id = session.workspace_id

    if auto_workspace_id == _ZERO_WORKSPACE_ID:
        auto_workspace_id = None

    if not auto_workspace_id:
        hint = (
            "Use --workspace-id, set [workspaces].cpu in config.toml, or set INSPIRE_WORKSPACE_ID."
            if gpu_count == 0
            else "Use --workspace-id, set [workspaces].gpu in config.toml, or set INSPIRE_WORKSPACE_ID."
        )
        _handle_error(
            ctx,
            "ConfigError",
            "No workspace_id configured.",
            EXIT_CONFIG_ERROR,
            hint=hint,
        )
        return None

    return auto_workspace_id


__all__ = ["resolve_notebook_workspace_id"]
