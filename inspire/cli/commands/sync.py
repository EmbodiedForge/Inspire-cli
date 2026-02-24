"""Sync command - Push local branch and sync code on Bridge.

Usage:
    inspire sync [--branch <branch>] [--remote <remote>] [--transport <ssh|workflow>]

This command:
1. Pushes the current (or specified) branch to the remote
2. Syncs code on Bridge via selected transport
3. Returns the synced commit SHA

If the git remote is unreachable, use 'inspire bridge scp' to transfer
files directly.
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
from inspire.cli.utils.output import (
    emit_error as emit_output_error,
    emit_success as emit_output_success,
)


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


def get_current_commit_sha(revision: str = "HEAD") -> str:
    """Get the commit SHA for a git revision (default: HEAD)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", revision],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or e.stdout or str(e)
        raise click.ClickException(f"Failed to get commit SHA for '{revision}': {error_msg}")


def get_commit_message(revision: str = "HEAD") -> str:
    """Get the commit message (first line) for a git revision."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s", revision],
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


def _effective_ssh_source(source: str, bridge: BridgeProfile) -> str:
    """Resolve SSH sync source based on user preference and bridge capability."""
    if source == "auto":
        return "remote" if bridge.has_internet else "bundle"
    return source


def _effective_push_mode(
    *,
    no_push: bool,
    push_mode: Optional[str],
    transport: str,
    ssh_source: Optional[str],
) -> str:
    """Resolve git push behavior before sync."""
    if no_push:
        return "skip"
    if push_mode:
        return push_mode
    if transport == "ssh" and ssh_source == "bundle":
        return "best-effort"
    return "required"


def _is_locale_warning(line: str) -> bool:
    """Return True when *line* is a known locale warning we intentionally hide."""
    return "setlocale:" in line and "cannot change locale" in line


def _normalize_sync_error(raw_error: object) -> str:
    """Normalize sync stderr/stdout text for user-facing reporting.

    - Drops locale warning chatter that does not affect sync behavior.
    - Removes duplicate lines while preserving first-seen order.
    """
    text = str(raw_error or "").strip()
    if not text:
        return "Unknown error"

    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _is_locale_warning(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)

    if not lines:
        return "Unknown error"
    return "\n".join(lines)


def _is_ff_divergence_error(error_text: str) -> bool:
    """Detect fast-forward divergence errors from git output."""
    lowered = error_text.lower()
    return (
        "not possible to fast-forward" in lowered
        or "diverging branches can't be fast-forwarded" in lowered
    )


def _summarize_sync_failure(
    *,
    raw_error: object,
    branch: str,
    remote: str,
) -> tuple[str, Optional[str], str]:
    """Build concise sync failure message + hint + normalized details."""
    normalized = _normalize_sync_error(raw_error)

    if _is_ff_divergence_error(normalized):
        message = f"Branch '{branch}' on Bridge diverged and cannot be fast-forwarded."
        hint = (
            f"Reconcile branch history (merge/rebase) and retry sync. "
            f"If you expected a fresh tip, push '{branch}' to '{remote}' first."
        )
        return message, hint, normalized

    first_line = normalized.splitlines()[0].strip() if normalized else "Unknown error"
    return first_line, None, normalized


def sync_via_tunnel(
    ctx: Context,
    config: Config,
    *,
    branch: str,
    commit_sha: str,
    commit_msg: str,
    remote: str,
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
            bridge_name=bridge_name,
            config=tunnel_config,
            timeout=timeout,
        )

    if result.get("success"):
        synced_sha = result.get("synced_sha") or commit_sha[:7]
        bundle_mode = result.get("bundle_mode") if offline_bundle else None
        bundle_base_sha = result.get("bundle_base_sha") if offline_bundle else None
        payload: dict[str, object] = {
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
        if bundle_mode:
            payload["bundle_mode"] = bundle_mode
        if bundle_base_sha:
            payload["bundle_base_sha"] = bundle_base_sha

        if ctx.debug and not ctx.json_output:
            click.echo(
                click.style("OK", fg="green")
                + f" Synced branch '{branch}' ({synced_sha[:7]}) to {config.target_dir}"
            )
            click.echo(f"  Commit: {commit_msg}")
            if offline_bundle:
                mode_suffix = f", {bundle_mode}" if bundle_mode else ""
                click.echo(f"  Method: SSH tunnel (offline bundle{mode_suffix})")
            else:
                click.echo("  Method: SSH tunnel (fast)")
        else:
            emit_output_success(
                ctx,
                payload=payload,
                text=f"synced {synced_sha[:7]} via {'ssh-bundle' if offline_bundle else 'ssh'}",
            )
        return EXIT_SUCCESS

    message, hint, details = _summarize_sync_failure(
        raw_error=result.get("error"),
        branch=branch,
        remote=remote,
    )

    human_lines = [f"Sync failed: {message}"]
    if hint:
        human_lines.append(f"Hint: {hint}")
    if ctx.debug and details and details != message:
        human_lines.append("Details:")
        human_lines.append(details)
    emit_output_error(
        ctx,
        error_type="SyncError",
        message=message,
        exit_code=EXIT_GENERAL_ERROR,
        hint=hint,
        human_lines=human_lines,
    )
    return EXIT_GENERAL_ERROR


def sync_via_workflow(
    ctx: Context,
    config: Config,
    *,
    branch: str,
    commit_sha: str,
    commit_msg: str,
    remote: str,
    wait: bool,
    timeout: int,
) -> int:
    """Sync code via Git Actions workflow transport."""
    if ctx.debug and not ctx.json_output:
        click.echo("Triggering sync workflow...")

    try:
        run_id = trigger_sync_workflow(config, branch, commit_sha)
    except (ForgeError, ForgeAuthError, GiteaError, GiteaAuthError) as e:
        emit_output_error(
            ctx,
            error_type="GiteaError",
            message=str(e),
            exit_code=EXIT_CONFIG_ERROR,
            human_lines=[f"Error: {e}"],
        )
        return EXIT_CONFIG_ERROR

    if wait and run_id:
        if ctx.debug and not ctx.json_output:
            click.echo("Waiting for sync to complete...")

        try:
            result = wait_for_workflow_completion(config, run_id, timeout)
        except TimeoutError:
            emit_output_error(
                ctx,
                error_type="Timeout",
                message=f"Sync workflow did not complete within {timeout}s",
                exit_code=EXIT_GENERAL_ERROR,
                hint="Check Gitea for sync workflow status.",
                human_lines=[
                    f"Sync workflow timed out after {timeout}s",
                    "The sync may still complete. Check Gitea for status.",
                ],
            )
            return EXIT_GENERAL_ERROR

        if result.get("conclusion") == "success":
            if ctx.debug and not ctx.json_output:
                click.echo(
                    click.style("OK", fg="green")
                    + f" Synced branch '{branch}' ({commit_sha[:7]}) to {config.target_dir}"
                )
                click.echo(f"  Commit: {commit_msg}")
                click.echo(f"  Remote: {remote}")
            else:
                emit_output_success(
                    ctx,
                    payload={
                        "status": "success",
                        "method": "gitea_actions",
                        "branch": branch,
                        "remote": remote,
                        "commit": commit_sha[:7],
                        "commit_full": commit_sha,
                        "message": commit_msg,
                        "target_dir": config.target_dir,
                        "html_url": result.get("html_url", ""),
                    },
                    text=f"synced {commit_sha[:7]} via workflow",
                )
            return EXIT_SUCCESS

        hint = result.get("html_url") or None
        human_lines = [f"Sync failed: {result.get('conclusion', 'unknown')}"]
        if result.get("html_url"):
            human_lines.append(f"  See: {result['html_url']}")
        emit_output_error(
            ctx,
            error_type="SyncError",
            message=f"Sync failed: {result.get('conclusion', 'unknown')}",
            exit_code=EXIT_GENERAL_ERROR,
            hint=hint,
            human_lines=human_lines,
        )
        return EXIT_GENERAL_ERROR

    if ctx.debug and not ctx.json_output:
        click.echo(click.style("OK", fg="green") + f" Pushed {branch} to {remote}")
        click.echo(
            click.style("OK", fg="green")
            + " Triggered sync workflow"
            + (f" (run {run_id})" if run_id else "")
        )
        click.echo(f"  Commit: {commit_sha[:7]} - {commit_msg}")
    else:
        emit_output_success(
            ctx,
            payload={
                "status": "triggered",
                "method": "gitea_actions",
                "branch": branch,
                "remote": remote,
                "commit": commit_sha[:7],
                "commit_full": commit_sha,
                "run_id": run_id,
            },
            text="triggered sync workflow" + (f" (run {run_id})" if run_id else ""),
        )

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
    help="Skip git push before sync (same as --push-mode skip)",
)
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Allow sync with uncommitted changes (syncs committed branch tip only)",
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
    "--source",
    type=click.Choice(["auto", "remote", "bundle"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="For SSH transport: choose sync source (auto uses remote on internet bridges, bundle otherwise)",
)
@click.option(
    "--push-mode",
    type=click.Choice(["required", "best-effort", "skip"], case_sensitive=False),
    default=None,
    help="Git push policy before sync (default: required for remote/workflow, best-effort for bundle)",
)
@pass_context
def sync(
    ctx: Context,
    branch: Optional[str],
    remote: Optional[str],
    no_push: bool,
    allow_dirty: bool,
    wait: bool,
    timeout: int,
    transport: str,
    source: str,
    push_mode: Optional[str],
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
        inspire sync --source bundle          # Force local bundle sync over SSH
        inspire sync --push-mode best-effort  # Continue even if git push fails
        inspire sync --no-push                # Skip git push (equivalent to --push-mode skip)
        inspire sync --allow-dirty            # Sync committed branch tip even if worktree is dirty

    \b
    Environment variables:
        INSPIRE_DEFAULT_REMOTE    Default git remote (default: origin)
        INSPIRE_TARGET_DIR        Target directory on Bridge (required)
    """
    try:
        config, _ = Config.from_files_and_env(require_target_dir=True, require_credentials=False)
    except ConfigError as e:
        emit_output_error(
            ctx,
            error_type="ConfigError",
            message=str(e),
            exit_code=EXIT_CONFIG_ERROR,
            human_lines=[f"Configuration error: {e}"],
        )
        sys.exit(EXIT_CONFIG_ERROR)

    # Determine branch
    if branch is None:
        branch = get_current_branch()

    # Determine remote
    if remote is None:
        remote = config.default_remote

    transport = transport.lower().strip()
    source = source.lower().strip()
    push_mode = push_mode.lower().strip() if push_mode else None

    if transport == "workflow" and source != "auto":
        emit_output_error(
            ctx,
            error_type="ValidationError",
            message="--source is only supported with '--transport ssh'",
            exit_code=EXIT_CONFIG_ERROR,
            human_lines=["Error: --source is only supported with '--transport ssh'."],
        )
        sys.exit(EXIT_CONFIG_ERROR)

    if no_push and push_mode and push_mode != "skip":
        emit_output_error(
            ctx,
            error_type="ValidationError",
            message="--no-push conflicts with --push-mode values other than 'skip'",
            exit_code=EXIT_CONFIG_ERROR,
            human_lines=["Error: --no-push conflicts with --push-mode values other than 'skip'."],
        )
        sys.exit(EXIT_CONFIG_ERROR)

    tunnel_config = None
    selected_bridge = None
    ssh_source = None
    use_offline_bundle = False
    candidate_bridges: list[BridgeProfile] = []
    if transport == "ssh":
        tunnel_config = load_tunnel_config()
        candidate_bridges = _ordered_bridges_for_sync(tunnel_config)
        if not candidate_bridges:
            hint = "Use 'inspire tunnel list' or 'inspire notebook ssh <id>' first."
            emit_output_error(
                ctx,
                error_type="TunnelUnavailable",
                message="No bridge configured for SSH sync",
                exit_code=EXIT_CONFIG_ERROR,
                hint=hint,
                human_lines=["Error: No bridge configured for SSH sync.", f"Hint: {hint}"],
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
            hint = "Run 'inspire tunnel status' or use '--transport workflow'."
            emit_output_error(
                ctx,
                error_type="TunnelUnavailable",
                message=(
                    "SSH tunnel is not available for any configured bridge " f"(tried: {tried_csv})"
                ),
                exit_code=EXIT_GENERAL_ERROR,
                hint=hint,
                human_lines=[
                    (
                        "Error: SSH tunnel is not available for any configured bridge "
                        f"(tried: {tried_csv})."
                    ),
                    f"Hint: {hint}",
                ],
            )
            sys.exit(EXIT_GENERAL_ERROR)

        ssh_source = _effective_ssh_source(source, selected_bridge)
        if ssh_source == "remote" and not selected_bridge.has_internet:
            hint = "Use '--source bundle' (or '--source auto') for no-internet bridges."
            emit_output_error(
                ctx,
                error_type="ValidationError",
                message=(
                    f"Bridge '{selected_bridge.name}' has no internet; "
                    "remote source is unavailable"
                ),
                exit_code=EXIT_CONFIG_ERROR,
                hint=hint,
                human_lines=[
                    (
                        f"Error: Bridge '{selected_bridge.name}' has no internet; "
                        "remote source is unavailable."
                    ),
                    f"Hint: {hint}",
                ],
            )
            sys.exit(EXIT_CONFIG_ERROR)

        use_offline_bundle = ssh_source == "bundle"
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
                if source == "bundle":
                    click.echo("Using offline bundle sync path (--source bundle).", err=True)
                else:
                    click.echo(
                        "Selected bridge has no internet; using offline bundle sync path.",
                        err=True,
                    )
            elif source == "remote":
                click.echo("Using remote git sync path (--source remote).")
    else:
        try:
            _preflight_workflow_transport(config)
        except (ForgeError, ForgeAuthError, ConfigError) as e:
            emit_output_error(
                ctx,
                error_type="ConfigError",
                message=str(e),
                exit_code=EXIT_CONFIG_ERROR,
                human_lines=[f"Configuration error: {e}"],
            )
            sys.exit(EXIT_CONFIG_ERROR)

    # Check for uncommitted changes
    if has_uncommitted_changes():
        if not allow_dirty:
            hint = "Commit/stash changes, or pass --allow-dirty to sync committed HEAD only."
            emit_output_error(
                ctx,
                error_type="ValidationError",
                message="Uncommitted changes detected",
                exit_code=EXIT_GENERAL_ERROR,
                hint=hint,
                human_lines=["Error: Uncommitted changes detected.", f"Hint: {hint}"],
            )
            sys.exit(EXIT_GENERAL_ERROR)

        if not ctx.json_output:
            click.echo(
                f"Warning: Uncommitted changes detected; syncing committed tip of '{branch}' only (--allow-dirty).",
                err=True,
            )

    commit_sha = get_current_commit_sha(branch)
    commit_msg = get_commit_message(branch)
    effective_push_mode = _effective_push_mode(
        no_push=no_push,
        push_mode=push_mode,
        transport=transport,
        ssh_source=ssh_source,
    )

    if effective_push_mode != "skip":
        try:
            push_to_remote(branch, remote, show_progress=ctx.debug and not ctx.json_output)
        except click.ClickException as e:
            if effective_push_mode == "best-effort":
                if not ctx.json_output:
                    click.echo(
                        f"Warning: {e}. Continuing because push mode is best-effort.",
                        err=True,
                    )
            else:
                if ctx.json_output:
                    emit_output_error(
                        ctx,
                        error_type="GitError",
                        message=str(e),
                        exit_code=EXIT_GENERAL_ERROR,
                    )
                    sys.exit(EXIT_GENERAL_ERROR)
                raise
    elif ctx.debug and not ctx.json_output:
        click.echo("Skipping git push before sync.")

    if transport == "ssh":
        exit_code = sync_via_tunnel(
            ctx,
            config,
            branch=branch,
            commit_sha=commit_sha,
            commit_msg=commit_msg,
            remote=remote,
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
        wait=wait,
        timeout=timeout,
    )
    sys.exit(exit_code)
