"""Sync command - Push local branch and sync code on Bridge.

Usage:
    inspire sync [--branch <branch>] [--remote <remote>]

This command:
1. Pushes the current (or specified) branch to the remote
2. Syncs code on Bridge via SSH tunnel (if available) or Gitea Actions
3. Returns the synced commit SHA
"""

import subprocess
import sys
import logging
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_SUCCESS,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
)
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.gitea import (
    GiteaError,
    GiteaAuthError,
    trigger_sync_workflow,
    wait_for_workflow_completion,
)
from inspire.cli.utils.tunnel import (
    is_tunnel_available,
    sync_via_ssh,
    TunnelNotAvailableError,
)
from inspire.cli.formatters import json_formatter


def _get_current_branch() -> str:
    """Get the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Failed to get current branch: {e.stderr}")
    except FileNotFoundError:
        raise click.ClickException("git command not found. Please install git.")


def _get_current_commit_sha() -> str:
    """Get the current commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Failed to get commit SHA: {e.stderr}")


def _get_commit_message() -> str:
    """Get the current commit message (first line)."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def _check_uncommitted_changes() -> bool:
    """Check if there are uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def _push_to_remote(branch: str, remote: str) -> None:
    """Push the branch to the remote."""
    click.echo(f"Pushing {branch} to {remote}...")
    try:
        result = subprocess.run(
            ["git", "push", remote, branch],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stderr:
            # Git push outputs to stderr even on success
            logging.debug(result.stderr)
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or e.stdout or str(e)
        raise click.ClickException(f"Failed to push to {remote}: {error_msg}")


@click.command()
@click.option(
    "--branch", "-b",
    default=None,
    help="Branch to sync (default: current branch)",
)
@click.option(
    "--remote", "-r",
    default=None,
    help="Git remote to push to (default: from INSPIRE_DEFAULT_REMOTE or 'origin')",
)
@click.option(
    "--no-push",
    is_flag=True,
    help="Skip git push, only trigger sync on Bridge",
)
@click.option(
    "--force", "-f",
    is_flag=True,
    help="Force sync on Bridge (git reset --hard), discarding any local changes there",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for sync to complete (default: wait)",
)
@click.option(
    "--timeout",
    default=120,
    help="Timeout in seconds when waiting for sync (default: 120)",
)
@pass_context
def sync(
    ctx: Context,
    branch: Optional[str],
    remote: Optional[str],
    no_push: bool,
    force: bool,
    wait: bool,
    timeout: int,
) -> None:
    """Sync local code to the Bridge shared filesystem.

    This command pushes your local branch to Gitea, then triggers a
    workflow on the self-hosted runner to sync the code to the shared
    filesystem used by the Inspire training platform.

    \b
    Examples:
        inspire sync                          # Sync current branch via origin
        inspire sync --remote upstream        # Sync via upstream remote
        inspire sync --branch feature/new     # Sync specific branch
        inspire sync --no-wait                # Don't wait for completion

    \b
    Environment variables:
        INSPIRE_DEFAULT_REMOTE    Default git remote (default: origin)
        INSPIRE_TARGET_DIR        Target directory on Bridge (required)
        INSP_GITEA_REPO           Gitea repo (owner/repo)
        INSP_GITEA_TOKEN          Gitea Personal Access Token
        INSP_GITEA_SERVER         Gitea server URL
    """
    try:
        # Load config - we need Gitea settings but not Inspire API credentials
        # for sync, so we do a minimal check
        config = Config.from_env_for_sync()
    except ConfigError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("ConfigError", str(e), EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(f"Configuration error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    # Determine branch
    if branch is None:
        branch = _get_current_branch()

    # Determine remote
    if remote is None:
        remote = config.default_remote

    # Check for uncommitted changes
    if _check_uncommitted_changes():
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ValidationError",
                    "Uncommitted changes detected",
                    EXIT_GENERAL_ERROR,
                    hint="Commit or stash your changes before syncing",
                ),
                err=True,
            )
            sys.exit(EXIT_GENERAL_ERROR)
        else:
            click.echo("Warning: You have uncommitted changes.", err=True)
            click.echo("These will NOT be synced. Commit or stash first.", err=True)
            if not click.confirm("Continue anyway?"):
                sys.exit(EXIT_GENERAL_ERROR)

    commit_sha = _get_current_commit_sha()
    commit_msg = _get_commit_message()

    # Push to remote (unless --no-push)
    if not no_push:
        try:
            _push_to_remote(branch, remote)
        except click.ClickException as e:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error("GitError", str(e), EXIT_GENERAL_ERROR),
                    err=True,
                )
                sys.exit(EXIT_GENERAL_ERROR)
            raise

    # Try SSH tunnel first (much faster), fall back to Gitea Actions
    if is_tunnel_available():
        _sync_via_tunnel(ctx, config, branch, commit_sha, commit_msg, remote, force, timeout)
    else:
        _sync_via_gitea(ctx, config, branch, commit_sha, commit_msg, remote, force, wait, timeout)

    sys.exit(EXIT_SUCCESS)


def _sync_via_tunnel(
    ctx: Context,
    config: Config,
    branch: str,
    commit_sha: str,
    commit_msg: str,
    remote: str,
    force: bool,
    timeout: int,
) -> None:
    """Sync code via SSH tunnel (fast path)."""
    if not ctx.json_output:
        click.echo("Syncing via SSH tunnel...")

    try:
        result = sync_via_ssh(
            target_dir=config.target_dir,
            branch=branch,
            commit_sha=commit_sha,
            force=force,
            timeout=timeout,
        )

        if result["success"]:
            synced_sha = result["synced_sha"] or commit_sha[:7]
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "status": "success",
                            "method": "ssh_tunnel",
                            "branch": branch,
                            "remote": remote,
                            "commit": commit_sha[:7],
                            "commit_full": commit_sha,
                            "synced_sha": synced_sha,
                            "message": commit_msg,
                            "target_dir": config.target_dir,
                        }
                    )
                )
            else:
                click.echo(click.style("OK", fg="green") + f" Synced branch '{branch}' ({synced_sha[:7]}) to {config.target_dir}")
                click.echo(f"  Commit: {commit_msg}")
                click.echo(f"  Method: SSH tunnel (fast)")
        else:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "SyncError",
                        str(result.get("error")),
                        EXIT_GENERAL_ERROR,
                    ),
                    err=True,
                )
            else:
                click.echo(f"Sync failed: {result['error']}", err=True)
            sys.exit(EXIT_GENERAL_ERROR)

    except TunnelNotAvailableError as e:
        # Fall back to Gitea Actions
        if not ctx.json_output:
            click.echo(f"SSH tunnel unavailable ({e}), falling back to Gitea Actions...")
        _sync_via_gitea(ctx, config, branch, commit_sha, commit_msg, remote, force, True, timeout)


def _sync_via_gitea(
    ctx: Context,
    config: Config,
    branch: str,
    commit_sha: str,
    commit_msg: str,
    remote: str,
    force: bool,
    wait: bool,
    timeout: int,
) -> None:
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
        sys.exit(EXIT_CONFIG_ERROR)

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
            sys.exit(EXIT_GENERAL_ERROR)

        if result["conclusion"] == "success":
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
                click.echo(click.style("OK", fg="green") + f" Synced branch '{branch}' ({commit_sha[:7]}) to {config.target_dir}")
                click.echo(f"  Commit: {commit_msg}")
                click.echo(f"  Remote: {remote}")
        else:
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
            sys.exit(EXIT_GENERAL_ERROR)
    else:
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
            click.echo(click.style("OK", fg="green") + " Triggered sync workflow" + (f" (run {run_id})" if run_id else ""))
            click.echo(f"  Commit: {commit_sha[:7]} - {commit_msg}")
