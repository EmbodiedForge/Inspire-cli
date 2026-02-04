"""Shared helpers for submitting jobs via the Inspire OpenAPI client."""

from __future__ import annotations

import os
from datetime import datetime

from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils import web_session as web_session_module
from inspire.cli.utils.browser_api import ProjectInfo
from inspire.config import Config, ConfigError, build_env_exports
from inspire.cli.utils.job_cache import JobCache


def wrap_in_bash(command: str) -> str:
    """Wrap a command in bash -c unless already wrapped."""
    stripped = command.strip()

    if stripped.startswith(("bash -c ", "sh -c ", "/bin/bash -c ", "/bin/sh -c ")):
        return command

    escaped = command.replace("'", "'\\''")
    return f"bash -c '{escaped}'"


def build_remote_logged_command(config: Config, *, command: str) -> tuple[str, str | None]:
    """Build the remote command (with optional logging) and return (final_command, log_path)."""
    env_exports = build_env_exports(config.remote_env)
    final_command = f"{env_exports}{command}" if env_exports else command

    log_path = None
    if config.target_dir:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(config.target_dir, ".inspire")
        log_filename = f"training_master_{timestamp}.log"
        log_path = os.path.join(log_dir, log_filename)
        final_command = (
            f'{env_exports}mkdir -p "{log_dir}" && ( cd "{config.target_dir}" && {command} ) '
            f'> "{log_path}" 2>&1'
        )

    return final_command, log_path


def select_project_for_workspace(
    config: Config,
    *,
    workspace_id: str,
    requested: str | None,
) -> tuple[ProjectInfo, str | None]:
    """Select a project for the given workspace, with quota-aware fallback."""
    try:
        session = web_session_module.get_web_session()
    except ValueError as e:
        raise ConfigError(str(e)) from e

    projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    if not projects:
        raise ConfigError("No projects available")
    return browser_api_module.select_project(projects, requested or config.job_project_id)


def cache_created_job(
    config: Config,
    *,
    job_id: str,
    name: str,
    resource: str,
    command: str,
    log_path: str | None,
) -> None:
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=job_id,
        name=name,
        resource=resource,
        command=command,
        status="PENDING",
        log_path=log_path,
    )
