"""Sync command - Push local branch and sync code on Bridge.

Usage:
    inspire sync [--branch <branch>] [--remote <remote>] [--transport <ssh|workflow>]

This command:
1. Pushes the current (or specified) branch to the remote
2. Syncs code on Bridge via selected transport
3. Returns the synced commit SHA

If the git remote is unreachable, use 'inspire bridge scp' to transfer
files directly. The --via-action flag is deprecated.
"""

from __future__ import annotations

import logging
import re
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
    ForgeAuthError,
    ForgeError,
    GiteaAuthError,
    GiteaError,
    _get_active_repo,
    create_forge_client,
    trigger_sync_workflow,
    wait_for_workflow_completion,
)
from inspire.bridge.tunnel import (
    BridgeProfile,
    TunnelConfig,
    is_tunnel_available,
    load_tunnel_config,
    sync_via_ssh,
    sync_via_ssh_bundle,
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


def push_to_remote(branch: str, remote: str, *, show_progress: bool = False) -> None:
    """Push the branch to the remote."""
    if show_progress:
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


def _preflight_workflow_transport(config: Config) -> None:
    """Validate workflow transport configuration without triggering side effects."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(1, 1)}"
    client.request_json("GET", runs_url)


def _is_cpu_bridge_name(name: str) -> bool:
    """Best-effort CPU bridge detection from profile name."""
    normalized = re.sub(r"[^a-z0-9]+", " ", name.lower())
    return "cpu" in normalized.split()


def _ordered_bridges_for_sync(tunnel_config: TunnelConfig) -> list[BridgeProfile]:
    """Return all configured bridges ordered for sync preference.

    Priority:
    1) internet + CPU
    2) internet + non-CPU
    3) no-internet + CPU
    4) no-internet + non-CPU
    """
    bridges = tunnel_config.list_bridges()
    if not bridges:
        return []

    default_bridge = tunnel_config.default_bridge

    def _priority(bridge: BridgeProfile) -> int:
        is_cpu = _is_cpu_bridge_name(bridge.name)
        if bridge.has_internet and is_cpu:
            return 0
        if bridge.has_internet:
            return 1
        if is_cpu:
            return 2
        return 3

    # Stable sort keeps insertion order among same-priority non-default bridges.
    return sorted(
        bridges,
        key=lambda bridge: (
            _priority(bridge),
            0 if bridge.name == default_bridge else 1,
        ),
    )


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
    offline_bundle: bool = False,
    bridge_name: Optional[str] = None,
    tunnel_config=None,
) -> int:
    """Sync code via SSH tunnel (fast path)."""
    if ctx.debug and not ctx.json_output:
        if bridge_name:
            click.echo(f"Syncing via SSH tunnel (bridge: {bridge_name})...")
        else:
            click.echo("Syncing via SSH tunnel...")

    if offline_bundle:
        result = sync_via_ssh_bundle(
            target_dir=config.target_dir,
            branch=branch,
            commit_sha=commit_sha,
            force=force,
            bridge_name=bridge_name,
            config=tunnel_config,
            timeout=timeout,
        )
    else:
        result = sync_via_ssh(
            target_dir=config.target_dir,
            branch=branch,
            commit_sha=commit_sha,
            remote=remote,
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
                        "method": "ssh_bundle" if offline_bundle else "ssh_tunnel",
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
            if ctx.debug:
                click.echo(
                    click.style("OK", fg="green")
                    + f" Synced branch '{branch}' ({synced_sha[:7]}) to {config.target_dir}"
                )
                click.echo(f"  Commit: {commit_msg}")
                click.echo(
                    "  Method: SSH tunnel (offline bundle)"
                    if offline_bundle
                    else "  Method: SSH tunnel (fast)"
                )
            else:
                click.echo(
                    f"synced {synced_sha[:7]} via {'ssh-bundle' if offline_bundle else 'ssh'}"
                )
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
    """Sync code via Git Actions workflow transport."""
    if ctx.debug and not ctx.json_output:
        click.echo("Triggering sync workflow...")

    try:
        run_id = trigger_sync_workflow(config, branch, commit_sha, force)
    except (ForgeError, ForgeAuthError, GiteaError, GiteaAuthError) as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("GiteaError", str(e), EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        return EXIT_CONFIG_ERROR

    if wait and run_id:
        if ctx.debug and not ctx.json_output:
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
                if ctx.debug:
                    click.echo(
                        click.style("OK", fg="green")
                        + f" Synced branch '{branch}' ({commit_sha[:7]}) to {config.target_dir}"
                    )
                    click.echo(f"  Commit: {commit_msg}")
                    click.echo(f"  Remote: {remote}")
                else:
                    click.echo(f"synced {commit_sha[:7]} via workflow")
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
        if ctx.debug:
            click.echo(click.style("OK", fg="green") + f" Pushed {branch} to {remote}")
            click.echo(
                click.style("OK", fg="green")
                + " Triggered sync workflow"
                + (f" (run {run_id})" if run_id else "")
            )
            click.echo(f"  Commit: {commit_sha[:7]} - {commit_msg}")
        else:
            click.echo("triggered sync workflow" + (f" (run {run_id})" if run_id else ""))

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
    "--allow-dirty",
    is_flag=True,
    help="Allow sync with uncommitted changes (syncs committed HEAD only)",
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
    "--transport",
    type=click.Choice(["ssh", "workflow"], case_sensitive=False),
    default="ssh",
    show_default=True,
    help="Sync transport to use (no automatic fallback)",
)
@click.option(
    "--via-action",
    is_flag=True,
    help="Deprecated alias for '--transport workflow'",
)
@pass_context
def sync(
    ctx: Context,
    branch: Optional[str],
    remote: Optional[str],
    no_push: bool,
    allow_dirty: bool,
    force: bool,
    wait: bool,
    timeout: int,
    transport: str,
    via_action: bool,
) -> None:
    """Sync local code to the Bridge shared filesystem.

    This command pushes your local branch to the remote, then syncs to Bridge
    using the selected transport:
    - ssh: direct SSH tunnel sync (default; uses offline bundle mode if bridge has no internet)
    - workflow: Git Actions workflow sync

    \b
    Examples:
        inspire sync                          # Sync current branch via SSH tunnel
        inspire sync --transport workflow     # Sync via workflow transport
        inspire sync --remote upstream        # Sync via upstream remote
        inspire sync --branch feature/new     # Sync specific branch
        inspire sync --no-push                # Skip git push, sync only
        inspire sync --allow-dirty            # Sync committed HEAD even if worktree is dirty
        inspire sync --force                  # Force reset on Bridge

    \b
    Environment variables:
        INSPIRE_DEFAULT_REMOTE    Default git remote (default: origin)
        INSPIRE_TARGET_DIR        Target directory on Bridge (required)
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

    # Determine branch
    if branch is None:
        branch = get_current_branch()

    # Determine remote
    if remote is None:
        remote = config.default_remote

    transport = transport.lower().strip()
    if via_action:
        transport = "workflow"
        if not ctx.json_output:
            click.echo(
                "Warning: --via-action is deprecated. Use '--transport workflow' instead.",
                err=True,
            )

    tunnel_config = None
    selected_bridge = None
    use_offline_bundle = False
    candidate_bridges: list[BridgeProfile] = []
    if transport == "ssh":
        tunnel_config = load_tunnel_config()
        candidate_bridges = _ordered_bridges_for_sync(tunnel_config)
        if not candidate_bridges:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "TunnelUnavailable",
                        "No bridge configured for SSH sync",
                        EXIT_CONFIG_ERROR,
                        hint="Use 'inspire tunnel list' or 'inspire notebook ssh <id>' first.",
                    ),
                    err=True,
                )
            else:
                click.echo("Error: No bridge configured for SSH sync.", err=True)
                click.echo(
                    "Hint: Use 'inspire tunnel list' or 'inspire notebook ssh <id>' first.",
                    err=True,
                )
            sys.exit(EXIT_CONFIG_ERROR)

        tried_bridges: list[str] = []
        for bridge in candidate_bridges:
            tried_bridges.append(bridge.name)
            if is_tunnel_available(
                bridge_name=bridge.name,
                config=tunnel_config,
                retries=config.tunnel_retries,
                retry_pause=config.tunnel_retry_pause,
            ):
                selected_bridge = bridge
                break

        if not selected_bridge:
            tried_csv = ", ".join(tried_bridges)
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "TunnelUnavailable",
                        f"SSH tunnel is not available for any configured bridge (tried: {tried_csv})",
                        EXIT_GENERAL_ERROR,
                        hint="Run 'inspire tunnel status' or use '--transport workflow'.",
                    ),
                    err=True,
                )
            else:
                click.echo(
                    f"Error: SSH tunnel is not available for any configured bridge (tried: {tried_csv}).",
                    err=True,
                )
                click.echo(
                    "Hint: Run 'inspire tunnel status' or use '--transport workflow'.",
                    err=True,
                )
            sys.exit(EXIT_GENERAL_ERROR)

        use_offline_bundle = not selected_bridge.has_internet
        if ctx.debug and not ctx.json_output:
            has_cpu_candidate = any(
                _is_cpu_bridge_name(bridge.name) for bridge in candidate_bridges
            )
            if _is_cpu_bridge_name(selected_bridge.name):
                click.echo(f"Using CPU bridge '{selected_bridge.name}' for sync.")
            elif has_cpu_candidate:
                click.echo(
                    f"CPU bridge unavailable, using '{selected_bridge.name}' for sync.",
                    err=True,
                )
            if use_offline_bundle:
                click.echo(
                    "Selected bridge has no internet; using offline bundle sync path.",
                    err=True,
                )
    else:
        try:
            _preflight_workflow_transport(config)
        except (ForgeError, ForgeAuthError, ConfigError) as e:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error("ConfigError", str(e), EXIT_CONFIG_ERROR),
                    err=True,
                )
            else:
                click.echo(f"Configuration error: {e}", err=True)
            sys.exit(EXIT_CONFIG_ERROR)

    # Check for uncommitted changes
    if has_uncommitted_changes():
        if not allow_dirty:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "ValidationError",
                        "Uncommitted changes detected",
                        EXIT_GENERAL_ERROR,
                        hint="Commit/stash changes, or pass --allow-dirty to sync committed HEAD only.",
                    ),
                    err=True,
                )
            else:
                click.echo("Error: Uncommitted changes detected.", err=True)
                click.echo(
                    "Hint: Commit/stash changes, or pass --allow-dirty to sync committed HEAD only.",
                    err=True,
                )
            sys.exit(EXIT_GENERAL_ERROR)

        if not ctx.json_output:
            click.echo(
                "Warning: Uncommitted changes detected; syncing committed HEAD only (--allow-dirty).",
                err=True,
            )

    commit_sha = get_current_commit_sha()
    commit_msg = get_commit_message()

    # Push to remote (unless --no-push)
    if not no_push:
        try:
            push_to_remote(branch, remote, show_progress=ctx.debug and not ctx.json_output)
        except click.ClickException as e:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error("GitError", str(e), EXIT_GENERAL_ERROR),
                    err=True,
                )
                sys.exit(EXIT_GENERAL_ERROR)
            raise

    if transport == "ssh":
        exit_code = sync_via_tunnel(
            ctx,
            config,
            branch=branch,
            commit_sha=commit_sha,
            commit_msg=commit_msg,
            remote=remote,
            force=force,
            timeout=timeout,
            offline_bundle=use_offline_bundle,
            bridge_name=selected_bridge.name,
            tunnel_config=tunnel_config,
        )
        sys.exit(exit_code)

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
