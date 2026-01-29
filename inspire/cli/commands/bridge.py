"""Bridge commands for executing raw commands on the Bridge runner."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_SUCCESS,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.cli.utils.config import Config, ConfigError, build_env_exports
from inspire.cli.utils.gitea import (
    GiteaError,
    GiteaAuthError,
    trigger_bridge_action_workflow,
    wait_for_bridge_action_completion,
    download_bridge_artifact,
    fetch_bridge_output_log,
)
from inspire.cli.utils.tunnel import (
    is_tunnel_available,
    run_ssh_command,
    get_ssh_command_args,
    load_tunnel_config,
    TunnelNotAvailableError,
)
from inspire.cli.formatters import json_formatter


def _split_denylist(items: tuple[str, ...]) -> list[str]:
    parts: list[str] = []
    for raw in items:
        for chunk in raw.replace("\r", "").replace("\n", ",").split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


@click.group()
def bridge() -> None:
    """Run commands on the Bridge runner (executes in INSPIRE_TARGET_DIR)."""


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
@click.option("timeout", "--timeout", type=int, default=None, help="Timeout in seconds (default: config value)")
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
    no_tunnel: bool,
) -> None:
    """Execute a command on the Bridge runner.

    Uses SSH tunnel if available (instant), otherwise falls back to Gitea Actions.

    COMMAND is the shell command to run on Bridge (in INSPIRE_TARGET_DIR).
    Command output (stdout/stderr) is automatically displayed after completion.

    \b
    Examples:
        inspire bridge exec "uv venv .venv"
        inspire bridge exec "pip install torch" --timeout 600
        inspire bridge exec "uv venv .venv" \\
            --artifact-path .venv --download ./local
        inspire bridge exec "python train.py" --no-wait
        inspire bridge exec "ls" --no-tunnel  # Force Gitea workflow
    """

    try:
        config, _ = Config.from_files_and_env(
            require_target_dir=True, require_credentials=False
        )
    except ConfigError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("ConfigError", str(e), EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(f"Configuration error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    action_timeout = timeout or config.bridge_action_timeout or 300

    # Try SSH tunnel first (unless --no-tunnel or artifacts requested)
    if not no_tunnel and not artifact_path and not download:
        try:
            if is_tunnel_available():
                if not ctx.json_output:
                    click.echo("Using SSH tunnel (fast path)")
                    click.echo(f"Command: {command}")
                    click.echo(f"Working dir: {config.target_dir}")

                # Build full command with env exports and cd to target dir
                env_exports = build_env_exports(config.remote_env)
                full_command = f'{env_exports}cd "{config.target_dir}" && {command}'

                # Execute via SSH
                result = run_ssh_command(
                    command=full_command,
                    timeout=action_timeout,
                    capture_output=True,
                )

                # Display output
                if not ctx.json_output:
                    click.echo("")
                    click.echo("--- Command Output ---")
                    if result.stdout:
                        click.echo(result.stdout, nl=False)
                    if result.stderr:
                        click.echo(result.stderr, nl=False)
                    click.echo("")
                    click.echo("--- End Output ---")
                    click.echo("")

                if result.returncode != 0:
                    if ctx.json_output:
                        click.echo(
                            json_formatter.format_json_error(
                                "CommandFailed",
                                f"Command failed with exit code {result.returncode}",
                                EXIT_GENERAL_ERROR,
                            ),
                            err=True,
                        )
                    else:
                        click.echo(f"Command failed with exit code {result.returncode}", err=True)
                    sys.exit(EXIT_GENERAL_ERROR)

                if ctx.json_output:
                    click.echo(
                        json_formatter.format_json(
                            {
                                "status": "success",
                                "method": "ssh_tunnel",
                                "returncode": result.returncode,
                                "output": result.stdout + result.stderr,
                            }
                        )
                    )
                else:
                    click.echo("OK Command completed successfully (via SSH)")

                sys.exit(EXIT_SUCCESS)

        except TunnelNotAvailableError:
            if not ctx.json_output:
                click.echo("Tunnel not available, using Gitea workflow...", err=True)
        except Exception as e:
            if not ctx.json_output:
                click.echo(f"SSH execution failed: {e}", err=True)
                click.echo("Falling back to Gitea workflow...", err=True)

    # Gitea workflow path (original implementation)

    # Prepend remote_env exports to command
    env_exports = build_env_exports(config.remote_env)
    workflow_command = f"{env_exports}{command}" if env_exports else command

    # Merge denylist from env + CLI
    merged_denylist: list[str] = []
    if config.bridge_action_denylist:
        merged_denylist.extend(config.bridge_action_denylist)
    merged_denylist.extend(_split_denylist(denylist))

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
        trigger_bridge_action_workflow(
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
        sys.exit(EXIT_GENERAL_ERROR)

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
        sys.exit(EXIT_SUCCESS)

    action_timeout = timeout or config.bridge_action_timeout or 300

    if not ctx.json_output:
        click.echo(f"Waiting for completion (timeout {action_timeout}s)...")

    try:
        result = wait_for_bridge_action_completion(
            config=config,
            request_id=request_id,
            timeout=action_timeout,
        )
    except TimeoutError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("Timeout", str(e), EXIT_TIMEOUT),
                err=True,
            )
        else:
            click.echo(f"Timeout: {e}", err=True)
        sys.exit(EXIT_TIMEOUT)
    except GiteaError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("GiteaError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    # Fetch and display command output
    output_log: Optional[str] = None
    try:
        output_log = fetch_bridge_output_log(config, request_id)
    except GiteaError:
        pass  # Output fetch is best-effort

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
        sys.exit(EXIT_GENERAL_ERROR)

    if download:
        if not ctx.json_output:
            click.echo(f"Downloading artifact to {download}...")
        try:
            download_bridge_artifact(config, request_id, Path(download))
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
            sys.exit(EXIT_GENERAL_ERROR)

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

    sys.exit(EXIT_SUCCESS)


@bridge.command("ssh")
@pass_context
def bridge_ssh(ctx: Context) -> None:
    """Open an interactive SSH shell to Bridge.

    Requires an active SSH tunnel. Start with: inspire tunnel start

    \b
    Example:
        inspire tunnel start
        inspire bridge ssh
    """
    try:
        config, _ = Config.from_files_and_env(
            require_target_dir=True, require_credentials=False
        )
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

    if not is_tunnel_available(tunnel_config):
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
        config=tunnel_config,
        remote_command=f'{env_exports}cd "{config.target_dir}" && exec $SHELL -l',
    )

    if not ctx.json_output:
        click.echo(f"Opening SSH connection to Bridge...")
        click.echo(f"Working directory: {config.target_dir}")
        click.echo("Press Ctrl+D or type 'exit' to disconnect")
        click.echo("")

    # Replace current process with SSH
    os.execvp(ssh_args[0], ssh_args)
