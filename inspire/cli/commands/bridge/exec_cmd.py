"""Bridge exec command -- execute a shell command on the Bridge runner."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

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
    load_tunnel_config,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.notebook_cli import require_web_session
from inspire.cli.utils.tunnel_reconnect import (
    load_ssh_public_key_material,
    rebuild_notebook_bridge_profile,
    retry_pause_seconds,
    should_attempt_ssh_reconnect,
)
from inspire.config.ssh_runtime import resolve_ssh_runtime_config


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


def _verbose_output(ctx: Context) -> bool:
    return not ctx.json_output and ctx.debug


def _emit_cli_error(
    ctx: Context,
    *,
    error_type: str,
    message: str,
    exit_code: int = EXIT_GENERAL_ERROR,
    hint: Optional[str] = None,
) -> int:
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(
                error_type,
                message,
                exit_code,
                hint=hint,
            ),
            err=True,
        )
    else:
        click.echo(f"Error: {message}", err=True)
        if hint:
            click.echo(f"Hint: {hint}", err=True)
    return exit_code


def _emit_command_failed(ctx: Context, *, returncode: int) -> int:
    message = f"Command failed with exit code {returncode}"
    if ctx.json_output:
        return _emit_cli_error(
            ctx,
            error_type="CommandFailed",
            message=message,
        )
    click.echo(message, err=True)
    return EXIT_GENERAL_ERROR


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
    reconnect_limit = max(0, int(getattr(config, "tunnel_retries", 0)))
    reconnect_pause = float(getattr(config, "tunnel_retry_pause", 0.0) or 0.0)
    reconnect_attempt = 0
    resolved_bridge_name = bridge_name
    force_rebuild = False
    opened_once = False
    ssh_execution_started = False
    web_session = None
    ssh_public_key = ""
    ssh_runtime = None
    full_command = _build_remote_command(
        command=command,
        target_dir=str(config.target_dir),
        remote_env=config.remote_env,
    )

    def _pause_before_retry() -> None:
        pause_s = retry_pause_seconds(
            reconnect_attempt,
            base_pause=reconnect_pause,
            progressive=True,
        )
        if pause_s > 0:
            time.sleep(pause_s)

    def _require_rebuild(
        bridge: object,
        tunnel_config: object,
        *,
        reason: str,
    ) -> Optional[int]:
        nonlocal reconnect_attempt, web_session, ssh_public_key, ssh_runtime, force_rebuild

        notebook_id = str(getattr(bridge, "notebook_id", "") or "").strip()
        if not notebook_id:
            hint = (
                "Run 'inspire tunnel status' to troubleshoot. "
                "If needed, re-create the bridge via "
                "'inspire notebook ssh <notebook-id> --save-as <name>'. "
                "If you intended to run via Git Actions instead, pass '--no-tunnel'."
            )
            return _emit_cli_error(
                ctx,
                error_type="TunnelError",
                message=(
                    "SSH tunnel not available. "
                    f"Bridge '{getattr(bridge, 'name', 'unknown')}' is not responding "
                    "(notebook may be stopped)."
                ),
                hint=hint,
            )

        if reconnect_attempt >= reconnect_limit:
            return _emit_cli_error(
                ctx,
                error_type="TunnelError",
                message="SSH tunnel not available",
                hint=(
                    "Auto-rebuild retries exhausted. Run 'inspire tunnel status' and "
                    "retry 'inspire notebook ssh <notebook-id> --save-as <name>'."
                ),
            )

        reconnect_attempt += 1
        if not ctx.json_output:
            click.echo(
                f"{reason} (attempt {reconnect_attempt}/{reconnect_limit})...",
                err=True,
            )

        try:
            if web_session is None:
                web_session = require_web_session(
                    ctx,
                    hint=(
                        "Automatic tunnel rebuild needs web authentication. "
                        "Set [auth].username and configure password via INSPIRE_PASSWORD "
                        'or [accounts."<username>"].password.'
                    ),
                )
            if not ssh_public_key:
                ssh_public_key = load_ssh_public_key_material()
            if ssh_runtime is None:
                ssh_runtime = resolve_ssh_runtime_config()
            rebuild_notebook_bridge_profile(
                bridge_name=str(getattr(bridge, "name")),
                bridge=bridge,
                tunnel_config=tunnel_config,
                session=web_session,
                ssh_public_key=ssh_public_key,
                ssh_runtime=ssh_runtime,
            )
            force_rebuild = False
            return None
        except (ValueError, ConfigError) as e:
            if reconnect_attempt >= reconnect_limit:
                return _emit_cli_error(
                    ctx,
                    error_type="TunnelError",
                    message=f"Automatic tunnel rebuild failed: {e}",
                    hint="Check credentials, SSH key, and notebook status, then retry.",
                )
            _pause_before_retry()
            return None
        except Exception as e:
            if reconnect_attempt >= reconnect_limit:
                return _emit_cli_error(
                    ctx,
                    error_type="TunnelError",
                    message=f"Automatic tunnel rebuild failed: {e}",
                    hint="Verify the notebook is RUNNING and retry.",
                )
            _pause_before_retry()
            return None

    def _should_retry_after_disconnect_code(
        *,
        returncode: int,
        tunnel_config: object,
        bridge_name_to_check: str,
    ) -> bool:
        """Retry non-interactive SSH only when 255 also coincides with tunnel loss.

        SSH uses exit code 255 both for transport failures and some command failures.
        To avoid re-running non-idempotent commands incorrectly, require a quick
        tunnel health probe to fail before attempting rebuild/retry.
        """
        if not should_attempt_ssh_reconnect(
            returncode,
            interactive=False,
            allow_non_interactive=True,
        ):
            return False

        try:
            tunnel_still_ready = is_tunnel_available_fn(
                bridge_name=bridge_name_to_check,
                config=tunnel_config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            )
        except Exception:
            # Conservatively treat probe errors as connectivity issues.
            return True

        return not tunnel_still_ready

    while True:
        try:
            tunnel_config = load_tunnel_config()
            bridge = tunnel_config.get_bridge(resolved_bridge_name)
            if bridge_name and bridge is None:
                return _emit_cli_error(
                    ctx,
                    error_type="ConfigError",
                    message=f"Bridge '{bridge_name}' not found.",
                    hint="Run 'inspire tunnel list' to see available bridge profiles.",
                )
            if bridge is None:
                return None

            resolved_bridge_name = str(getattr(bridge, "name"))
            availability_retries = 0 if force_rebuild else int(config.tunnel_retries)
            availability_pause = 0.0 if force_rebuild else float(config.tunnel_retry_pause)
            tunnel_ready = is_tunnel_available_fn(
                bridge_name=resolved_bridge_name,
                config=tunnel_config,
                retries=availability_retries,
                retry_pause=availability_pause,
                progressive=not force_rebuild,
            )

            if force_rebuild or not tunnel_ready:
                reconnect_error = _require_rebuild(
                    bridge,
                    tunnel_config,
                    reason=(
                        "SSH connection dropped; rebuilding tunnel automatically"
                        if force_rebuild
                        else "Tunnel unavailable; rebuilding automatically"
                    ),
                )
                if reconnect_error is not None:
                    return reconnect_error
                continue

            if ctx.json_output:
                ssh_execution_started = True
                result = run_ssh_command_fn(
                    command=full_command,
                    bridge_name=resolved_bridge_name,
                    timeout=timeout_s,
                    capture_output=True,
                )
                returncode = getattr(result, "returncode", 1)
                if returncode == 0:
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

                if _should_retry_after_disconnect_code(
                    returncode=returncode,
                    tunnel_config=tunnel_config,
                    bridge_name_to_check=resolved_bridge_name,
                ):
                    force_rebuild = True
                    continue

                return _emit_command_failed(ctx, returncode=returncode)

            if _verbose_output(ctx) and not opened_once:
                click.echo("Using SSH tunnel (fast path)")
                click.echo(f"Bridge: {resolved_bridge_name}")
                click.echo(f"Command: {command}")
                click.echo(f"Working dir: {config.target_dir}")
                click.echo("--- Command Output ---")
                opened_once = True

            ssh_execution_started = True
            exit_code = run_ssh_command_streaming_fn(
                command=full_command,
                bridge_name=resolved_bridge_name,
                timeout=timeout_s,
            )
            if _verbose_output(ctx):
                click.echo("--- End Output ---")

            if exit_code == 0:
                click.echo("OK")
                return EXIT_SUCCESS

            if _should_retry_after_disconnect_code(
                returncode=exit_code,
                tunnel_config=tunnel_config,
                bridge_name_to_check=resolved_bridge_name,
            ):
                force_rebuild = True
                continue

            return _emit_command_failed(ctx, returncode=exit_code)

        except TunnelNotAvailableError as e:
            if ssh_execution_started:
                return _emit_cli_error(
                    ctx,
                    error_type="TunnelError",
                    message=f"SSH execution failed: {e}",
                )
            force_rebuild = True
            continue
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
            if ssh_execution_started:
                return _emit_cli_error(
                    ctx,
                    error_type="SSHExecutionError",
                    message=f"SSH execution failed: {e}",
                )
            if _verbose_output(ctx):
                click.echo(f"SSH execution failed: {e}", err=True)
                click.echo("Falling back to Gitea workflow...", err=True)
                click.echo(
                    "Warning: Git Actions fallback is deprecated and will be removed "
                    "in a future release. Use SSH tunnel instead.",
                    err=True,
                )
            elif not ctx.json_output:
                click.echo("SSH execution failed, using workflow fallback", err=True)
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

    if not merged_denylist and _verbose_output(ctx):
        click.echo("Warning: no denylist provided; proceeding", err=True)

    request_id = f"{int(time.time())}-{os.getpid()}"
    artifact_paths_list = list(artifact_path)

    if _verbose_output(ctx):
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
            click.echo(f"Triggered bridge exec request {request_id}")
        return EXIT_SUCCESS

    if _verbose_output(ctx):
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
        if _verbose_output(ctx):
            click.echo("")
            click.echo("--- Command Output ---")
            click.echo(output_log)
            click.echo("--- End Output ---")
            click.echo("")
        else:
            click.echo(output_log)

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
        if _verbose_output(ctx):
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
        if _verbose_output(ctx):
            click.echo("OK Action completed successfully")
            if result.get("html_url"):
                click.echo(f"Workflow: {result.get('html_url')}")
            if download:
                click.echo("Artifacts downloaded")
        elif download:
            click.echo("OK (artifacts downloaded)")
        else:
            click.echo("OK")

    return EXIT_SUCCESS


@click.command("exec")
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
@click.option("--no-tunnel", is_flag=True, help="Force use of Gitea workflow (deprecated)")
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
