"""Bridge commands for executing raw commands on the Bridge runner."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.config import Config, ConfigError, build_env_exports
from inspire.bridge.forge import (
    GiteaAuthError,
    GiteaError,
    trigger_bridge_action_workflow,
    wait_for_bridge_action_completion,
    download_bridge_artifact,
    fetch_bridge_output_log,
)
from inspire.bridge.tunnel import (
    TunnelNotAvailableError,
    is_tunnel_available,
    run_ssh_command,
    run_ssh_command_streaming,
    get_ssh_command_args,
    load_tunnel_config,
)
from inspire.cli.formatters import json_formatter


@click.group()
def bridge() -> None:
    """Run commands on the Bridge runner (executes in INSPIRE_TARGET_DIR)."""


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
    bridge_name: Optional[str],
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
            bridge_name=bridge_name,
            retries=config.tunnel_retries,
            retry_pause=config.tunnel_retry_pause,
        ):
            tunnel_config = load_tunnel_config()
            bridge = tunnel_config.get_bridge(bridge_name)
            if bridge_name and bridge is None:
                message = f"Bridge '{bridge_name}' not found."
                hint = "Run 'inspire tunnel list' to see available bridge profiles."
                if ctx.json_output:
                    click.echo(
                        json_formatter.format_json_error(
                            "ConfigError",
                            message,
                            EXIT_GENERAL_ERROR,
                            hint=hint,
                        ),
                        err=True,
                    )
                else:
                    click.echo(f"Error: {message}", err=True)
                    click.echo(f"Hint: {hint}", err=True)
                return EXIT_GENERAL_ERROR

            if bridge:
                hint = (
                    "Run 'inspire tunnel status' to troubleshoot. "
                    "If you intended to run via Git Actions instead, pass '--no-tunnel'."
                )
                if ctx.json_output:
                    click.echo(
                        json_formatter.format_json_error(
                            "TunnelError",
                            (
                                "SSH tunnel not available. "
                                f"Bridge '{bridge.name}' is not responding (notebook may be stopped)."
                            ),
                            EXIT_GENERAL_ERROR,
                            hint=hint,
                        ),
                        err=True,
                    )
                else:
                    click.echo(
                        (
                            "Error: SSH tunnel not available. "
                            f"Bridge '{bridge.name}' is not responding (notebook may be stopped)."
                        ),
                        err=True,
                    )
                    click.echo(f"Hint: {hint}", err=True)
                return EXIT_GENERAL_ERROR

            return None

        full_command = _build_remote_command(
            command=command,
            target_dir=str(config.target_dir),
            remote_env=config.remote_env,
        )

        if ctx.json_output:
            result = run_ssh_command_fn(
                command=full_command,
                bridge_name=bridge_name,
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
        if bridge_name:
            click.echo(f"Bridge: {bridge_name}")
        click.echo(f"Command: {command}")
        click.echo(f"Working dir: {config.target_dir}")
        click.echo("")
        click.echo("--- Command Output ---")

        exit_code = run_ssh_command_streaming_fn(
            command=full_command,
            bridge_name=bridge_name,
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
            click.echo(json_formatter.format_json_error("Timeout", str(e), EXIT_TIMEOUT), err=True)
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


@bridge.command("exec")
@click.argument("command")
@click.option(
    "denylist",
    "--denylist",
    multiple=True,
    help="Denylist pattern to block (repeatable or comma-separated)",
)
@click.option(
    "artifact_path",
    "--artifact-path",
    multiple=True,
    help="Path relative to INSPIRE_TARGET_DIR to upload as artifact (repeatable)",
)
@click.option(
    "download",
    "--download",
    type=click.Path(),
    help="Local directory to download artifact contents",
)
@click.option("wait", "--wait/--no-wait", default=True, help="Wait for completion (default: wait)")
@click.option(
    "timeout",
    "--timeout",
    type=int,
    default=None,
    help="Timeout in seconds (default: config value)",
)
@click.option(
    "bridge",
    "--bridge",
    "-b",
    help="Bridge profile to use for SSH tunnel execution",
)
@click.option("--no-tunnel", is_flag=True, help="Force use of Gitea workflow (skip SSH tunnel)")
@pass_context
def exec_command(
    ctx: Context,
    command: str,
    denylist: tuple[str, ...],
    artifact_path: tuple[str, ...],
    download: Optional[str],
    wait: bool,
    timeout: Optional[int],
    bridge: Optional[str],
    no_tunnel: bool,
) -> None:
    """Execute a command on the Bridge runner.

    Uses SSH tunnel if available (instant). If a bridge is configured but not responding,
    exits with an error (the notebook may be stopped). Use --no-tunnel to force Git Actions.

    COMMAND is the shell command to run on Bridge (in INSPIRE_TARGET_DIR).
    Command output (stdout/stderr) is automatically displayed after completion.

    \b
    Examples:
        inspire bridge exec "uv venv .venv"
        inspire bridge exec "pip install torch" --timeout 600
        inspire bridge exec "uv venv .venv" \\
            --artifact-path .venv --download ./local
        inspire bridge exec "python train.py" --no-wait
        inspire bridge exec "hostname" --bridge qz-bridge
        inspire bridge exec "ls" --no-tunnel  # Force Gitea workflow
    """

    try:
        config, _ = Config.from_files_and_env(require_target_dir=True, require_credentials=False)
    except ConfigError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("ConfigError", str(e), EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(f"Configuration error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    action_timeout = int(timeout) if timeout is not None else int(config.bridge_action_timeout)

    # Try SSH tunnel first (unless --no-tunnel or artifacts requested)
    if not no_tunnel and not artifact_path and not download:
        ssh_exit_code = try_exec_via_ssh_tunnel(
            ctx,
            command=command,
            bridge_name=bridge,
            config=config,
            timeout_s=action_timeout,
            is_tunnel_available_fn=is_tunnel_available,
            run_ssh_command_fn=run_ssh_command,
            run_ssh_command_streaming_fn=run_ssh_command_streaming,
        )
        if ssh_exit_code is not None:
            sys.exit(ssh_exit_code)

    workflow_exit_code = exec_via_workflow(
        ctx,
        command=command,
        denylist=denylist,
        artifact_path=artifact_path,
        download=download,
        wait=wait,
        timeout_s=action_timeout,
        config=config,
        trigger_bridge_action_workflow_fn=trigger_bridge_action_workflow,
        wait_for_bridge_action_completion_fn=wait_for_bridge_action_completion,
        fetch_bridge_output_log_fn=fetch_bridge_output_log,
        download_bridge_artifact_fn=download_bridge_artifact,
    )
    sys.exit(workflow_exit_code)


@bridge.command("ssh")
@click.option("--bridge", "-b", help="Bridge profile to connect")
@pass_context
def bridge_ssh(ctx: Context, bridge: Optional[str]) -> None:
    """Open an interactive SSH shell to Bridge.

    Requires an active SSH tunnel. Start with: inspire tunnel start

    \b
    Example:
        inspire tunnel start
        inspire bridge ssh
    """
    try:
        config, _ = Config.from_files_and_env(require_target_dir=True, require_credentials=False)
    except ConfigError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("ConfigError", str(e), EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(f"Configuration error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    tunnel_config = load_tunnel_config()

    if not is_tunnel_available(bridge_name=bridge, config=tunnel_config):
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "TunnelError",
                    "SSH tunnel not available",
                    EXIT_GENERAL_ERROR,
                    hint="Run 'inspire tunnel start' first",
                ),
                err=True,
            )
        else:
            click.echo("Error: SSH tunnel not available", err=True)
            click.echo("Hint: Run 'inspire tunnel start' first", err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    # Build interactive SSH command with env exports and cd to target dir
    env_exports = build_env_exports(config.remote_env)
    ssh_args = get_ssh_command_args(
        bridge_name=bridge,
        config=tunnel_config,
        remote_command=f'{env_exports}cd "{config.target_dir}" && exec $SHELL -l',
    )

    if not ctx.json_output:
        click.echo("Opening SSH connection to Bridge...")
        if bridge:
            click.echo(f"Bridge: {bridge}")
        click.echo(f"Working directory: {config.target_dir}")
        click.echo("Press Ctrl+D or type 'exit' to disconnect")
        click.echo("")

    # Replace current process with SSH
    os.execvp(ssh_args[0], ssh_args)
