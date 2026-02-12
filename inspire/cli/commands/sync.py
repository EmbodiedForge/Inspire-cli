"""Sync command - Push local branch and sync code on Bridge.

Usage:
    inspire sync [--branch <branch>] [--remote <remote>]

This command:
1. Pushes the current (or specified) branch to the remote
2. Syncs code on Bridge via SSH tunnel (requires active tunnel)
3. Returns the synced commit SHA

Pass --via-action to allow fallback to Gitea/GitHub Actions when the
tunnel is unavailable.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
)
from inspire.config import Config, ConfigError
from inspire.bridge.forge import (
    GiteaAuthError,
    GiteaError,
    trigger_sync_workflow,
    wait_for_workflow_completion,
)
from inspire.bridge.tunnel import (
    is_tunnel_available,
    load_tunnel_config,
    sync_via_ssh,
)
from inspire.cli.formatters import json_formatter


def get_current_branch() -> str:
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


def get_current_commit_sha() -> str:
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


def get_commit_message() -> str:
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


def has_uncommitted_changes() -> bool:
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


def push_to_remote(branch: str, remote: str) -> None:
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
            logging.debug(result.stderr)
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or e.stdout or str(e)
        raise click.ClickException(f"Failed to push to {remote}: {error_msg}")


def sync_via_tunnel(
    ctx: Context,
    config: Config,
    *,
    branch: str,
    commit_sha: str,
    commit_msg: str,
    remote: str,
    force: bool,
    timeout: int,
    bridge_name: Optional[str] = None,
    tunnel_config=None,
) -> int:
    """Sync code via SSH tunnel (fast path)."""
    if not ctx.json_output:
        if bridge_name:
            click.echo(f"Syncing via SSH tunnel (bridge: {bridge_name})...")
        else:
            click.echo("Syncing via SSH tunnel...")

    result = sync_via_ssh(
        target_dir=config.target_dir,
        branch=branch,
        commit_sha=commit_sha,
        force=force,
        bridge_name=bridge_name,
        config=tunnel_config,
        timeout=timeout,
    )

    if result.get("success"):
        synced_sha = result.get("synced_sha") or commit_sha[:7]
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
            click.echo(
                click.style("OK", fg="green")
                + f" Synced branch '{branch}' ({synced_sha[:7]}) to {config.target_dir}"
            )
            click.echo(f"  Commit: {commit_msg}")
            click.echo("  Method: SSH tunnel (fast)")
        return EXIT_SUCCESS

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
        click.echo(f"Sync failed: {result.get('error')}", err=True)
    return EXIT_GENERAL_ERROR


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


@click.command()
@click.option(
    "--branch",
    "-b",
    default=None,
    help="Branch to sync (default: current branch)",
)
@click.option(
    "--remote",
    "-r",
    default=None,
    help="Git remote to push to (default: from INSPIRE_DEFAULT_REMOTE or 'origin')",
)
@click.option(
    "--no-push",
    is_flag=True,
    help="Skip git push, only trigger sync on Bridge",
)
@click.option(
    "--force",
    "-f",
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
@click.option(
    "--via-action",
    is_flag=True,
    help="Allow fallback to Gitea/GitHub Actions workflow if SSH tunnel is unavailable",
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
    via_action: bool,
) -> None:
    """Sync local code to the Bridge shared filesystem.

    This command pushes your local branch to the remote, then syncs the
    code on Bridge via SSH tunnel. An SSH tunnel must be active.

    To fall back to Gitea/GitHub Actions workflow when the tunnel is
    unavailable, pass --via-action.

    \b
    Examples:
        inspire sync                          # Sync current branch via SSH tunnel
        inspire sync --via-action             # Allow action fallback if tunnel is down
        inspire sync --remote upstream        # Sync via upstream remote
        inspire sync --branch feature/new     # Sync specific branch
        inspire sync --no-wait                # Don't wait for completion (action path)

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
        branch = get_current_branch()

    # Determine remote
    if remote is None:
        remote = config.default_remote

    # Check for uncommitted changes
    if has_uncommitted_changes():
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

    commit_sha = get_current_commit_sha()
    commit_msg = get_commit_message()

    # Push to remote (unless --no-push)
    if not no_push:
        try:
            push_to_remote(branch, remote)
        except click.ClickException as e:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error("GitError", str(e), EXIT_GENERAL_ERROR),
                    err=True,
                )
                sys.exit(EXIT_GENERAL_ERROR)
            raise

    # Try SSH tunnel first (much faster)
    # For sync, we need a bridge with internet access (for git fetch)
    tunnel_config = load_tunnel_config()
    internet_bridge = tunnel_config.get_bridge_with_internet()

    if internet_bridge and is_tunnel_available(
        bridge_name=internet_bridge.name, config=tunnel_config
    ):
        exit_code = sync_via_tunnel(
            ctx,
            config,
            branch=branch,
            commit_sha=commit_sha,
            commit_msg=commit_msg,
            remote=remote,
            force=force,
            timeout=timeout,
            bridge_name=internet_bridge.name,
            tunnel_config=tunnel_config,
        )
        sys.exit(exit_code)

    # SSH tunnel not available
    if not via_action:
        # No fallback allowed — error out
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "TunnelUnavailable",
                    "SSH tunnel is not available and --via-action was not specified",
                    EXIT_GENERAL_ERROR,
                ),
                err=True,
            )
        else:
            if not internet_bridge:
                click.echo("Error: No bridge with internet access configured.", err=True)
            else:
                click.echo("Error: SSH tunnel is not available.", err=True)
            click.echo(
                "Hint: Use --via-action to fall back to Gitea/GitHub Actions workflow.",
                err=True,
            )
        sys.exit(EXIT_GENERAL_ERROR)

    # --via-action: fall back to workflow
    if not ctx.json_output:
        if tunnel_config.bridges and not internet_bridge:
            click.echo("Warning: No bridge with internet access configured.", err=True)
        click.echo("Falling back to Gitea/GitHub Actions workflow.", err=True)
    exit_code = sync_via_workflow(
        ctx,
        config,
        branch=branch,
        commit_sha=commit_sha,
        commit_msg=commit_msg,
        remote=remote,
        force=force,
        wait=wait,
        timeout=timeout,
    )
    sys.exit(exit_code)
