"""Sync command - Push local branch and sync code on Bridge.

Usage:
    inspire sync [--branch <branch>] [--remote <remote>]

This command:
1. Pushes the current (or specified) branch to the remote
2. Triggers a GitLab pipeline on the Bridge runner to sync the code
3. Returns the synced commit SHA
"""

import json
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
from inspire.cli.utils.gitlab import (
    GitLabError,
    GitLabAuthError,
    trigger_sync_pipeline,
    wait_for_pipeline_completion,
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

    This command pushes your local branch to GitLab, then triggers a
    pipeline on the self-hosted runner to sync the code to the shared
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
        INSP_GITLAB_PROJECT       GitLab project (namespace/project)
        INSP_GITLAB_TOKEN         GitLab Personal Access Token
        INSP_GITLAB_SERVER        GitLab server URL (default: https://gitlab.com)
    """
    try:
        # Load config - we need GitLab settings but not Inspire API credentials
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

    # Trigger sync pipeline
    if not ctx.json_output:
        click.echo("Triggering sync pipeline...")

    try:
        pipeline_response = trigger_sync_pipeline(config, branch, commit_sha, force)
        pipeline_id = pipeline_response.get("id")
        pipeline_url = pipeline_response.get("web_url", "")
    except (GitLabError, GitLabAuthError) as e:
        if ctx.json_output:
            click.echo(json.dumps({
                "error": str(e),
                "type": "gitlab_error",
            }))
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    if wait and pipeline_id:
        if not ctx.json_output:
            click.echo("Waiting for sync to complete...")

        try:
            result = wait_for_pipeline_completion(config, pipeline_id, timeout)
        except TimeoutError:
            if ctx.json_output:
                click.echo(json.dumps({
                    "status": "timeout",
                    "branch": branch,
                    "commit": commit_sha[:7],
                    "error": f"Sync pipeline did not complete within {timeout}s",
                }))
            else:
                click.echo(f"⚠ Sync pipeline timed out after {timeout}s", err=True)
                click.echo("The sync may still complete. Check GitLab for status.", err=True)
            sys.exit(EXIT_GENERAL_ERROR)

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
                    "pipeline_url": result.get("web_url", ""),
                }))
            else:
                click.echo(click.style("✓", fg="green") + f" Synced branch '{branch}' ({commit_sha[:7]}) to {config.target_dir}")
                click.echo(f"  Commit: {commit_msg}")
                click.echo(f"  Remote: {remote}")
        else:
            if ctx.json_output:
                click.echo(json.dumps({
                    "status": "failed",
                    "branch": branch,
                    "commit": commit_sha[:7],
                    "conclusion": result.get("conclusion"),
                    "pipeline_url": result.get("web_url", ""),
                }))
            else:
                click.echo(f"✗ Sync failed: {result.get('conclusion', 'unknown')}", err=True)
                if result.get("web_url"):
                    click.echo(f"  See: {result['web_url']}", err=True)
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
                "pipeline_id": pipeline_id,
                "pipeline_url": pipeline_url,
            }))
        else:
            click.echo(click.style("✓", fg="green") + f" Pushed {branch} to {remote}")
            click.echo(click.style("✓", fg="green") + " Triggered sync pipeline" + (f" (pipeline {pipeline_id})" if pipeline_id else ""))
            click.echo(f"  Commit: {commit_sha[:7]} - {commit_msg}")
            if pipeline_url:
                click.echo(f"  Pipeline: {pipeline_url}")

    sys.exit(EXIT_SUCCESS)
