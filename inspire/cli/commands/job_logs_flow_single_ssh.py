"""SSH tunnel fast-path for `inspire job logs` (single-job mode)."""

from __future__ import annotations

import click

from inspire.cli.commands.job_logs_flow_single_output import echo_log_path, echo_ssh_content
from inspire.cli.commands.job_logs_helpers import _fetch_log_via_ssh, _follow_logs_via_ssh
from inspire.cli.context import Context, EXIT_GENERAL_ERROR, EXIT_SUCCESS
from inspire.cli.utils.config import Config
from inspire.cli.utils.tunnel import TunnelNotAvailableError, is_tunnel_available


def try_get_ssh_exit_code(
    ctx: Context,
    *,
    config: Config,
    job_id: str,
    remote_log_path: str,
    tail: int,
    head: int,
    path: bool,
    follow: bool,
) -> int | None:
    """If the SSH tunnel path is available, handle the request and return an exit code."""
    try:
        if is_tunnel_available():
            if follow:
                if not ctx.json_output:
                    click.echo("Using SSH tunnel (fast path)")

                final_status = _follow_logs_via_ssh(
                    job_id=job_id,
                    config=config,
                    remote_log_path=remote_log_path,
                    tail_lines=tail or 50,
                )

                if final_status in {"SUCCEEDED", "job_succeeded"}:
                    return EXIT_SUCCESS
                if final_status in {"FAILED", "CANCELLED", "job_failed", "job_cancelled"}:
                    return EXIT_GENERAL_ERROR
                return EXIT_SUCCESS

            if not ctx.json_output:
                click.echo("Using SSH tunnel (fast path)")

            content = _fetch_log_via_ssh(remote_log_path=remote_log_path, tail=tail, head=head)

            if path:
                echo_log_path(ctx, job_id=job_id, remote_log_path=remote_log_path)
            else:
                echo_ssh_content(
                    ctx,
                    job_id=job_id,
                    remote_log_path=remote_log_path,
                    content=content,
                    tail=tail,
                    head=head,
                )

            return EXIT_SUCCESS

    except TunnelNotAvailableError:
        if not ctx.json_output:
            click.echo("Tunnel not available, using Gitea workflow...", err=True)
    except IOError as e:
        if not ctx.json_output:
            click.echo(f"SSH log fetch failed: {e}", err=True)
            click.echo("Falling back to Gitea workflow...", err=True)

    return None
