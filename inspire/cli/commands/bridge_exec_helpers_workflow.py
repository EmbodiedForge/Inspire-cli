"""Workflow execution for `inspire bridge exec`."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

import click

from inspire.cli.context import Context, EXIT_GENERAL_ERROR, EXIT_SUCCESS, EXIT_TIMEOUT
from inspire.cli.formatters import json_formatter
from inspire.config import Config, build_env_exports
from inspire.cli.utils.gitea import GiteaAuthError, GiteaError


def split_denylist(items: tuple[str, ...]) -> list[str]:
    parts: list[str] = []
    for raw in items:
        for chunk in raw.replace("\r", "").replace("\n", ",").split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


def exec_via_workflow(
    ctx: Context,
    *,
    command: str,
    denylist: tuple[str, ...],
    artifact_path: tuple[str, ...],
    download: Optional[str],
    wait: bool,
    timeout_s: int,
    config: Config,
    trigger_bridge_action_workflow_fn: Callable[..., None],
    wait_for_bridge_action_completion_fn: Callable[..., dict],
    fetch_bridge_output_log_fn: Callable[..., Optional[str]],
    download_bridge_artifact_fn: Callable[..., None],
) -> int:
    """Execute Bridge command via the workflow path."""
    env_exports = build_env_exports(config.remote_env)
    workflow_command = f"{env_exports}{command}" if env_exports else command

    merged_denylist: list[str] = []
    if config.bridge_action_denylist:
        merged_denylist.extend(config.bridge_action_denylist)
    merged_denylist.extend(split_denylist(denylist))

    if not merged_denylist and not ctx.json_output:
        click.echo("Warning: no denylist provided; proceeding", err=True)

    request_id = f"{int(time.time())}-{os.getpid()}"
    artifact_paths_list = list(artifact_path)

    if not ctx.json_output:
        click.echo(f"Triggering bridge exec (request {request_id})")
        click.echo(f"Command: {command}")
        click.echo(f"Working dir: {config.target_dir}")
        if merged_denylist:
            click.echo(f"Denylist: {merged_denylist}")
        if artifact_paths_list:
            click.echo(f"Artifact paths: {artifact_paths_list}")

    try:
        trigger_bridge_action_workflow_fn(
            config=config,
            raw_command=workflow_command,
            artifact_paths=artifact_paths_list,
            request_id=request_id,
            denylist=merged_denylist,
        )
    except (GiteaError, GiteaAuthError) as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("GiteaError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        return EXIT_GENERAL_ERROR

    if not wait:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "status": "triggered",
                        "request_id": request_id,
                        "command": command,
                    }
                )
            )
        else:
            click.echo("Workflow dispatched; not waiting for completion")
        return EXIT_SUCCESS

    if not ctx.json_output:
        click.echo(f"Waiting for completion (timeout {timeout_s}s)...")

    try:
        result = wait_for_bridge_action_completion_fn(
            config=config,
            request_id=request_id,
            timeout=timeout_s,
        )
    except TimeoutError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("Timeout", str(e), EXIT_TIMEOUT),
                err=True,
            )
        else:
            click.echo(f"Timeout: {e}", err=True)
        return EXIT_TIMEOUT
    except GiteaError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("GiteaError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        return EXIT_GENERAL_ERROR

    output_log: Optional[str] = None
    try:
        output_log = fetch_bridge_output_log_fn(config, request_id)
    except GiteaError:
        pass

    if output_log and not ctx.json_output:
        click.echo("")
        click.echo("--- Command Output ---")
        click.echo(output_log)
        click.echo("--- End Output ---")
        click.echo("")

    if result.get("conclusion") != "success":
        if ctx.json_output:
            hint = result.get("html_url") or None
            click.echo(
                json_formatter.format_json_error(
                    "BridgeActionFailed",
                    f"Action failed: {result.get('conclusion')}",
                    EXIT_GENERAL_ERROR,
                    hint=hint,
                ),
                err=True,
            )
        else:
            click.echo(
                f"Action failed: {result.get('conclusion')} (see {result.get('html_url', '')})",
                err=True,
            )
        return EXIT_GENERAL_ERROR

    if download:
        if not ctx.json_output:
            click.echo(f"Downloading artifact to {download}...")
        try:
            download_bridge_artifact_fn(config, request_id, Path(download))
        except GiteaError as e:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "ArtifactError",
                        f"Artifact download failed: {e}",
                        EXIT_GENERAL_ERROR,
                    ),
                    err=True,
                )
            else:
                click.echo(f"Warning: artifact download failed: {e}", err=True)
            return EXIT_GENERAL_ERROR

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "success",
                    "request_id": request_id,
                    "artifact_downloaded": bool(download),
                    "output": output_log,
                }
            )
        )
    else:
        click.echo("OK Action completed successfully")
        if result.get("html_url"):
            click.echo(f"Workflow: {result.get('html_url')}")
        if download:
            click.echo("Artifacts downloaded")

    return EXIT_SUCCESS


__all__ = ["exec_via_workflow", "split_denylist"]
