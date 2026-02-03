"""Sync command - Push local branch and sync code on Bridge.

Usage:
    inspire sync [--branch <branch>] [--remote <remote>]

This command:
1. Pushes the current (or specified) branch to the remote
2. Syncs code on Bridge via SSH tunnel (if available) or Gitea Actions
3. Returns the synced commit SHA
"""

import sys
from typing import Optional

import click

from inspire.cli.commands.sync_git_helpers import (
    get_commit_message,
    get_current_branch,
    get_current_commit_sha,
    has_uncommitted_changes,
    push_to_remote,
)
from inspire.cli.commands.sync_tunnel_helpers import sync_via_tunnel
from inspire.cli.commands.sync_workflow_helpers import sync_via_workflow
from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
)
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.tunnel import (
    is_tunnel_available,
    load_tunnel_config,
)
from inspire.cli.formatters import json_formatter


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

    # Try SSH tunnel first (much faster), fall back to Gitea Actions
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
    else:
        # Fall back to Gitea Actions
        if not ctx.json_output and tunnel_config.bridges and not internet_bridge:
            click.echo("Warning: No bridge with internet access configured.", err=True)
            click.echo("Falling back to Gitea Actions for sync.", err=True)
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
