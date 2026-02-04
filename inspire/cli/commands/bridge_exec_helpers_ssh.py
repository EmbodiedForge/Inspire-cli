"""SSH tunnel execution for `inspire bridge exec`."""

from __future__ import annotations

import subprocess
from typing import Callable, Optional

import click

from inspire.cli.context import Context, EXIT_GENERAL_ERROR, EXIT_SUCCESS, EXIT_TIMEOUT
from inspire.cli.formatters import json_formatter
from inspire.config import Config, build_env_exports
from inspire.cli.utils.tunnel import TunnelNotAvailableError


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


__all__ = ["try_exec_via_ssh_tunnel"]
