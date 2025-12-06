"""Sync command - Push local branch and sync code on Bridge.

Usage:
    inspire sync [--branch <branch>] [--remote <remote>]

This command:
1. Pushes the current (or specified) branch to the remote
2. Triggers a GitHub Action on the Bridge runner to sync the code
3. Returns the synced commit SHA
"""

import json
import subprocess
import sys
import time
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
from inspire.cli.utils.github import (
    GitHubError,
    GitHubAuthError,
    _get_client,
    _get_repo,
)


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


def _trigger_sync_workflow(
    config: Config,
    branch: str,
    commit_sha: str,
    force: bool = False,
) -> str:
    """Trigger the sync workflow on GitHub Actions.

    Returns the workflow run ID.
    """
    repo = _get_repo(config)
    client = _get_client(config)

    workflow_file = config.sync_workflow
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"

    data = {
        "ref": "main",  # Workflow file must be on main
        "inputs": {
            "branch": branch,
            "commit_sha": commit_sha,
            "force": str(force).lower(),
            "target_dir": config.target_dir,
        },
    }

    try:
        client.request_json("POST", url, data=data)
    except GitHubError as e:
        raise click.ClickException(f"Failed to trigger sync workflow: {e}")

    # Wait briefly then find the run ID
    time.sleep(2)

    # Get the most recent workflow run for this workflow
    runs_url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs?per_page=5"
    try:
        response = client.request_json("GET", runs_url)
        runs = response.get("workflow_runs", [])
        if runs:
            return str(runs[0]["id"])
    except GitHubError:
        pass

    return ""


def _wait_for_sync_completion(
    config: Config,
    run_id: str,
    timeout: int = 120,
) -> dict:
    """Wait for the sync workflow to complete.

    Returns the workflow run result with status and conclusion.
    """
    if not run_id:
        return {"status": "unknown", "conclusion": None}

    repo = _get_repo(config)
    client = _get_client(config)

    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            response = client.request_json("GET", url)
            status = response.get("status")
            conclusion = response.get("conclusion")

            if status == "completed":
                return {
                    "status": status,
                    "conclusion": conclusion,
                    "html_url": response.get("html_url", ""),
                }

            time.sleep(3)
        except GitHubError as e:
            logging.debug(f"Error checking workflow status: {e}")
            time.sleep(3)

    return {"status": "timeout", "conclusion": None}


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

    This command pushes your local branch to GitHub, then triggers a
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
        INSPIRE_SYNC_WORKFLOW     Workflow filename (default: sync_code.yml)
        INSPIRE_TARGET_DIR        Target directory on Bridge (required)
    """
    try:
        # Load config - we need GitHub settings but not Inspire API credentials
        # for sync, so we do a minimal check
        config = Config.from_env_for_sync()
    except ConfigError as e:
        if ctx.json_output:
            click.echo(json.dumps({"error": str(e), "type": "config_error"}))
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
            click.echo(json.dumps({
                "error": "Uncommitted changes detected",
                "type": "validation_error",
                "hint": "Commit or stash your changes before syncing",
            }))
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
                click.echo(json.dumps({
                    "error": str(e),
                    "type": "git_error",
                }))
            raise

    # Trigger sync workflow
    if not ctx.json_output:
        click.echo("Triggering sync workflow...")

    try:
        run_id = _trigger_sync_workflow(config, branch, commit_sha, force)
    except (GitHubError, GitHubAuthError) as e:
        if ctx.json_output:
            click.echo(json.dumps({
                "error": str(e),
                "type": "github_error",
            }))
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    if wait and run_id:
        if not ctx.json_output:
            click.echo("Waiting for sync to complete...")

        result = _wait_for_sync_completion(config, run_id, timeout)

        if result["conclusion"] == "success":
            if ctx.json_output:
                click.echo(json.dumps({
                    "status": "success",
                    "branch": branch,
                    "remote": remote,
                    "commit": commit_sha[:7],
                    "commit_full": commit_sha,
                    "message": commit_msg,
                    "target_dir": config.target_dir,
                    "workflow_url": result.get("html_url", ""),
                }))
            else:
                click.echo(click.style("✓", fg="green") + f" Synced branch '{branch}' ({commit_sha[:7]}) to {config.target_dir}")
                click.echo(f"  Commit: {commit_msg}")
                click.echo(f"  Remote: {remote}")
        elif result["status"] == "timeout":
            if ctx.json_output:
                click.echo(json.dumps({
                    "status": "timeout",
                    "branch": branch,
                    "commit": commit_sha[:7],
                    "error": f"Sync workflow did not complete within {timeout}s",
                }))
            else:
                click.echo(f"⚠ Sync workflow timed out after {timeout}s", err=True)
                click.echo("The sync may still complete. Check GitHub Actions for status.", err=True)
            sys.exit(EXIT_GENERAL_ERROR)
        else:
            if ctx.json_output:
                click.echo(json.dumps({
                    "status": "failed",
                    "branch": branch,
                    "commit": commit_sha[:7],
                    "conclusion": result.get("conclusion"),
                    "workflow_url": result.get("html_url", ""),
                }))
            else:
                click.echo(f"✗ Sync failed: {result.get('conclusion', 'unknown')}", err=True)
                if result.get("html_url"):
                    click.echo(f"  See: {result['html_url']}", err=True)
            sys.exit(EXIT_GENERAL_ERROR)
    else:
        # Not waiting
        if ctx.json_output:
            click.echo(json.dumps({
                "status": "triggered",
                "branch": branch,
                "remote": remote,
                "commit": commit_sha[:7],
                "commit_full": commit_sha,
                "run_id": run_id,
            }))
        else:
            click.echo(click.style("✓", fg="green") + f" Pushed {branch} to {remote}")
            click.echo(click.style("✓", fg="green") + " Triggered sync workflow" + (f" (run {run_id})" if run_id else ""))
            click.echo(f"  Commit: {commit_sha[:7]} - {commit_msg}")

    sys.exit(EXIT_SUCCESS)
