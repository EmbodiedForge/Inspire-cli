"""Bridge commands for executing raw commands on the Bridge runner."""

from __future__ import annotations

import os
import sys
from typing import Optional

import click

from inspire.cli.commands.bridge_exec_helpers import exec_via_workflow, try_exec_via_ssh_tunnel
from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.utils.config import Config, ConfigError, build_env_exports
from inspire.cli.utils.gitea import (
    trigger_bridge_action_workflow,
    wait_for_bridge_action_completion,
    download_bridge_artifact,
    fetch_bridge_output_log,
)
from inspire.cli.utils.tunnel import (
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

    if not is_tunnel_available(config=tunnel_config):
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
        click.echo("Opening SSH connection to Bridge...")
        click.echo(f"Working directory: {config.target_dir}")
        click.echo("Press Ctrl+D or type 'exit' to disconnect")
        click.echo("")

    # Replace current process with SSH
    os.execvp(ssh_args[0], ssh_args)
