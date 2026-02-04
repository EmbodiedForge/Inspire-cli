"""Workflow sync helpers for `inspire sync`."""

from __future__ import annotations

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, EXIT_SUCCESS
from inspire.cli.formatters import json_formatter
from inspire.config import Config
from inspire.cli.utils.gitea import (
    GiteaAuthError,
    GiteaError,
    trigger_sync_workflow,
    wait_for_workflow_completion,
)


def sync_via_workflow(
    ctx: Context,
    config: Config,
    *,
    branch: str,
    commit_sha: str,
    commit_msg: str,
    remote: str,
    force: bool,
    wait: bool,
    timeout: int,
) -> int:
    """Sync code via Gitea Actions workflow (slower fallback)."""
    if not ctx.json_output:
        click.echo("Triggering sync workflow...")

    try:
        run_id = trigger_sync_workflow(config, branch, commit_sha, force)
    except (GiteaError, GiteaAuthError) as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("GiteaError", str(e), EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        return EXIT_CONFIG_ERROR

    if wait and run_id:
        if not ctx.json_output:
            click.echo("Waiting for sync to complete...")

        try:
            result = wait_for_workflow_completion(config, run_id, timeout)
        except TimeoutError:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "Timeout",
                        f"Sync workflow did not complete within {timeout}s",
                        EXIT_GENERAL_ERROR,
                        hint="Check Gitea for sync workflow status.",
                    ),
                    err=True,
                )
            else:
                click.echo(f"Sync workflow timed out after {timeout}s", err=True)
                click.echo("The sync may still complete. Check Gitea for status.", err=True)
            return EXIT_GENERAL_ERROR

        if result.get("conclusion") == "success":
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "status": "success",
                            "method": "gitea_actions",
                            "branch": branch,
                            "remote": remote,
                            "commit": commit_sha[:7],
                            "commit_full": commit_sha,
                            "message": commit_msg,
                            "target_dir": config.target_dir,
                            "html_url": result.get("html_url", ""),
                        }
                    )
                )
            else:
                click.echo(
                    click.style("OK", fg="green")
                    + f" Synced branch '{branch}' ({commit_sha[:7]}) to {config.target_dir}"
                )
                click.echo(f"  Commit: {commit_msg}")
                click.echo(f"  Remote: {remote}")
            return EXIT_SUCCESS

        if ctx.json_output:
            hint = result.get("html_url") or None
            click.echo(
                json_formatter.format_json_error(
                    "SyncError",
                    f"Sync failed: {result.get('conclusion', 'unknown')}",
                    EXIT_GENERAL_ERROR,
                    hint=hint,
                ),
                err=True,
            )
        else:
            click.echo(f"Sync failed: {result.get('conclusion', 'unknown')}", err=True)
            if result.get("html_url"):
                click.echo(f"  See: {result['html_url']}", err=True)
        return EXIT_GENERAL_ERROR

    # Not waiting
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "triggered",
                    "method": "gitea_actions",
                    "branch": branch,
                    "remote": remote,
                    "commit": commit_sha[:7],
                    "commit_full": commit_sha,
                    "run_id": run_id,
                }
            )
        )
    else:
        click.echo(click.style("OK", fg="green") + f" Pushed {branch} to {remote}")
        click.echo(
            click.style("OK", fg="green")
            + " Triggered sync workflow"
            + (f" (run {run_id})" if run_id else "")
        )
        click.echo(f"  Commit: {commit_sha[:7]} - {commit_msg}")

    return EXIT_SUCCESS
