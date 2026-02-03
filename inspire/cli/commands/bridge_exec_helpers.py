"""Helpers for `inspire bridge exec`.

Kept separate so `inspire/cli/commands/bridge.py` can stay small and focused on Click wiring.

Important: The Click command module (`bridge.py`) is monkeypatched by tests, so this helper
accepts dependency callables (tunnel + workflow) as parameters instead of importing them
directly. This keeps tests able to patch `inspire.cli.commands.bridge.<name>` as before.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.config import Config, build_env_exports
from inspire.cli.utils.gitea import GiteaAuthError, GiteaError
from inspire.cli.utils.tunnel import TunnelNotAvailableError


def split_denylist(items: tuple[str, ...]) -> list[str]:
    parts: list[str] = []
    for raw in items:
        for chunk in raw.replace("\r", "").replace("\n", ",").split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


def _build_remote_command(*, command: str, target_dir: str, remote_env: dict[str, str]) -> str:
    env_exports = build_env_exports(remote_env)
    return f'{env_exports}cd "{target_dir}" && {command}'


def try_exec_via_ssh_tunnel(
    ctx: Context,
    *,
    command: str,
    config: Config,
    timeout_s: int,
    is_tunnel_available_fn: Callable[..., bool],
    run_ssh_command_fn: Callable[..., object],
    run_ssh_command_streaming_fn: Callable[..., int],
) -> Optional[int]:
    """Attempt the fast-path SSH tunnel execution.

    Returns:
        Exit code if the SSH path handled the request (success/failure/timeout),
        otherwise None to fall back to workflow execution.
    """
    try:
        if not is_tunnel_available_fn(
            retries=config.tunnel_retries,
            retry_pause=config.tunnel_retry_pause,
        ):
            return None

        full_command = _build_remote_command(
            command=command,
            target_dir=str(config.target_dir),
            remote_env=config.remote_env,
        )

        if ctx.json_output:
            result = run_ssh_command_fn(
                command=full_command,
                timeout=timeout_s,
                capture_output=True,
            )

            returncode = getattr(result, "returncode", 1)
            if returncode != 0:
                click.echo(
                    json_formatter.format_json_error(
                        "CommandFailed",
                        f"Command failed with exit code {returncode}",
                        EXIT_GENERAL_ERROR,
                    ),
                    err=True,
                )
                return EXIT_GENERAL_ERROR

            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            click.echo(
                json_formatter.format_json(
                    {
                        "status": "success",
                        "method": "ssh_tunnel",
                        "returncode": returncode,
                        "output": stdout + stderr,
                    }
                )
            )
            return EXIT_SUCCESS

        click.echo("Using SSH tunnel (fast path)")
        click.echo(f"Command: {command}")
        click.echo(f"Working dir: {config.target_dir}")
        click.echo("")
        click.echo("--- Command Output ---")

        exit_code = run_ssh_command_streaming_fn(
            command=full_command,
            timeout=timeout_s,
        )

        click.echo("--- End Output ---")
        click.echo("")

        if exit_code != 0:
            click.echo(f"Command failed with exit code {exit_code}", err=True)
            return EXIT_GENERAL_ERROR

        click.echo("OK Command completed successfully (via SSH)")
        return EXIT_SUCCESS

    except TunnelNotAvailableError:
        if not ctx.json_output:
            click.echo("Tunnel not available, using Gitea workflow...", err=True)
        return None
    except subprocess.TimeoutExpired:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "Timeout",
                    f"Command timed out after {timeout_s}s",
                    EXIT_TIMEOUT,
                ),
                err=True,
            )
        else:
            click.echo(f"Command timed out after {timeout_s}s", err=True)
        return EXIT_TIMEOUT
    except Exception as e:
        if not ctx.json_output:
            click.echo(f"SSH execution failed: {e}", err=True)
            click.echo("Falling back to Gitea workflow...", err=True)
        return None


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
