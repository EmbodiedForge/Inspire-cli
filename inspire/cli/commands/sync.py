"""Sync command - Sync code on Bridge over the SSH tunnel.

Usage:
    inspire sync [--remote <remote>]

This command:
1. Syncs code on Bridge via the SSH tunnel
2. Optionally pushes the current branch to the remote (disabled by default)
3. Returns the synced commit SHA

If the git remote is unreachable, use 'inspire bridge scp' to transfer
files directly.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional, SupportsFloat, SupportsIndex, cast

import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.bridge_transport import BRIDGE_TRANSPORTS
from inspire.cli.utils.jupyter_bridge import JupyterBridgeClient
from inspire.cli.utils.notebook_cli import require_web_session, resolve_json_output
from inspire.config import Config, ConfigError
from inspire.bridge.tunnel import (
    BridgeProfile,
    TunnelConfig,
    is_tunnel_available,
    load_tunnel_config,
    save_tunnel_config,
    sync_via_ssh,
    sync_via_ssh_bundle,
)
from inspire.bridge.tunnel.sync import (
    FileTransferRunner,
    RemoteCommandRunner,
    audit_remote_sync_state,
    collect_remote_sync_diffs,
    pull_from_bridge,
)
from inspire.cli.utils.output import (
    emit_error,
    emit_info,
    emit_progress,
    emit_success,
    emit_warning,
)

logger = logging.getLogger(__name__)

_SYNC_BRIDGE_PROBE_TIMEOUT = 2
_DEFAULT_LARGE_FILE_THRESHOLD_MB = 100
_BYTES_PER_MIB = 1024 * 1024
_MAX_OVERLAP_DIFF_FILES = 2
_MAX_DIFF_LINES = 80


def _git_cwd_kwargs(local_cwd: str | None) -> dict[str, Any]:
    """Build the optional ``cwd`` kwarg for local git helpers.

    Callers pass it only when ``--local`` selects a repository root, so helpers
    keep running in the process working directory by default.
    """
    return {} if local_cwd is None else {"cwd": local_cwd}


def _resolve_local_repo_root(ctx: Context, local_path: Optional[str]) -> Optional[str]:
    """Validate a ``--local`` repository root and return it, or None for the process cwd."""
    if local_path is None:
        return None

    candidate = Path(local_path).expanduser()
    if not candidate.is_dir():
        emit_error(
            ctx,
            error_type="ValidationError",
            message=f"Local repository path is not a directory: {local_path}",
            exit_code=EXIT_VALIDATION_ERROR,
            hint="Pass --local a git working tree to read the branch and commit from.",
        )
        sys.exit(EXIT_VALIDATION_ERROR)

    resolved = str(candidate.resolve())
    detail = ""
    inside_work_tree = False
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            cwd=resolved,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        detail = str(exc)
    else:
        inside_work_tree = probe.returncode == 0 and probe.stdout.strip() == "true"
        detail = (probe.stderr or probe.stdout or "").strip()

    if not inside_work_tree:
        emit_error(
            ctx,
            error_type="ValidationError",
            message=f"Local repository path is not a git working tree: {resolved}",
            exit_code=EXIT_VALIDATION_ERROR,
            hint=detail or "Run 'git init' there, or omit --local to use the current directory.",
        )
        sys.exit(EXIT_VALIDATION_ERROR)

    return resolved


def get_current_branch(cwd: str | None = None) -> str:
    """Get the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Failed to get current branch: {e.stderr}") from e
    except FileNotFoundError:
        # Nothing useful comes from the FileNotFoundError traceback here: the fix is
        # to install git, so do not chain an irrelevant exception.
        raise click.ClickException("git command not found. Please install git.") from None


def get_current_commit_sha(revision: str = "HEAD", cwd: str | None = None) -> str:
    """Get the commit SHA for a git revision (default: HEAD)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", revision],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or e.stdout or str(e)
        raise click.ClickException(f"Failed to get commit SHA for '{revision}': {error_msg}") from e


def get_commit_message(revision: str = "HEAD", cwd: str | None = None) -> str:
    """Get the commit message (first line) for a git revision."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s", revision],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def has_uncommitted_changes(cwd: str | None = None) -> bool:
    """Check if there are uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def push_to_remote(
    branch: str, remote: str, *, show_progress: bool = False, cwd: str | None = None
) -> None:
    """Push the branch to the remote."""
    if show_progress:
        click.echo(f"Pushing {branch} to {remote}...")
    try:
        result = subprocess.run(
            ["git", "push", remote, branch],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        if result.stderr:
            logger.debug("git push stderr: %s", result.stderr)
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or e.stdout or str(e)
        raise click.ClickException(f"Failed to push to {remote}: {error_msg}") from e


def _ordered_bridges_for_sync(tunnel_config: TunnelConfig) -> list[BridgeProfile]:
    """Return configured bridges with the default bridge first, then config order."""
    bridges = tunnel_config.list_bridges()
    if not bridges:
        return []

    default_bridge = tunnel_config.default_bridge
    return sorted(
        bridges,
        key=lambda bridge: 0 if bridge.name == default_bridge else 1,
    )


def _effective_push_mode(
    *,
    no_push: bool,
    push_mode: Optional[str],
) -> str:
    """Resolve git push behavior before sync."""
    if no_push:
        return "skip"
    if push_mode:
        return push_mode
    return "skip"


def _probe_live_bridges(
    *,
    tunnel_config: TunnelConfig,
    config: Config,
    candidate_bridges: list[BridgeProfile],
) -> tuple[list[BridgeProfile], list[str]]:
    """Probe all candidate bridges in parallel and return the live subset in order."""
    tried_bridges = [bridge.name for bridge in candidate_bridges]
    if not candidate_bridges:
        return [], tried_bridges

    def _probe(bridge: BridgeProfile) -> bool:
        return is_tunnel_available(
            bridge_name=bridge.name,
            config=tunnel_config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
            probe_timeout=_SYNC_BRIDGE_PROBE_TIMEOUT,
        )

    live_by_name: dict[str, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(candidate_bridges)) as pool:
        future_map = {pool.submit(_probe, bridge): bridge.name for bridge in candidate_bridges}
        for future in concurrent.futures.as_completed(future_map):
            bridge_name = future_map[future]
            try:
                live_by_name[bridge_name] = future.result()
            except Exception:
                live_by_name[bridge_name] = False

    live_bridges = [bridge for bridge in candidate_bridges if live_by_name.get(bridge.name, False)]
    return live_bridges, tried_bridges


def _eligible_live_bridges(
    *,
    source: str,
    live_bridges: list[BridgeProfile],
) -> list[BridgeProfile]:
    """Return live bridges eligible for the requested sync source."""
    if source == "remote":
        return [bridge for bridge in live_bridges if bridge.has_internet]
    return list(live_bridges)


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
            "To pull Bridge changes first, rerun with --pull. "
            "To overwrite Bridge with your selected commit, rerun with --force. "
            "Or reconcile branch history manually and retry."
        )
        return message, hint, normalized

    first_line = normalized.splitlines()[0].strip() if normalized else "Unknown error"
    return first_line, None, normalized


def _is_sync_timeout_error(raw_error: object) -> bool:
    """Return True when sync failure text indicates a timeout."""
    normalized = _normalize_sync_error(raw_error).lower()
    return "timed out after" in normalized


def _human_size(num_bytes: object) -> str:
    try:
        size = float(cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, num_bytes))
    except (TypeError, ValueError):
        return "unknown size"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _parse_git_path_output(stdout: bytes) -> set[str]:
    return {path.decode("utf-8", errors="replace") for path in (stdout or b"").split(b"\0") if path}


def _local_changed_paths(
    base_revision: str | None, revision: str, cwd: str | None = None
) -> set[str]:
    """Return local paths changed between a remote base and selected sync commit."""
    if not base_revision:
        return set()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", base_revision, revision],
            check=False,
            capture_output=True,
            cwd=cwd,
        )
    except (subprocess.SubprocessError, OSError):
        return set()
    if result.returncode != 0:
        return set()
    return _parse_git_path_output(result.stdout or b"")


def _local_blob_hash(revision: str, path: str, cwd: str | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"{revision}:{path}"],
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    blob_hash = (result.stdout or "").strip().lower()
    if len(blob_hash) != 40 or any(ch not in "0123456789abcdef" for ch in blob_hash):
        return None
    return blob_hash


def _local_dirty_paths(cwd: str | None = None) -> set[str]:
    """Return local uncommitted paths, best-effort."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            check=False,
            capture_output=True,
            cwd=cwd,
        )
    except (subprocess.SubprocessError, OSError):
        return set()
    if result.returncode != 0:
        return set()

    paths: set[str] = set()
    entries = iter((result.stdout or b"").split(b"\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        status = entry[:2]
        path = entry[3:].decode("utf-8", errors="replace")
        if status[:1] in {b"R", b"C"}:
            next(entries, None)
        if path:
            paths.add(path)
    return paths


def _cap_diff(diff_text: str, *, max_lines: int = _MAX_DIFF_LINES) -> str:
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text.strip()
    truncated = lines[:max_lines]
    truncated.append(f"... diff truncated after {max_lines} lines")
    return "\n".join(truncated).strip()


def _literal_pathspec(path: str) -> str:
    """A pathspec that matches *path* exactly: no glob characters, no directory prefix.

    A bare path is also a glob pattern, so a dirty file named ``data[1].py`` would
    otherwise pull ``data1.py`` into its diff and miss its own.
    """
    return f":(literal){path}"


def _run_git_diff(args: list[str], cwd: str | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _local_overlap_diff(
    *,
    path: str,
    remote_head: str | None,
    commit_sha: str,
    sync_overlap: bool,
    dirty_overlap: bool,
    local_cwd: str | None = None,
) -> str:
    cwd_kwargs = _git_cwd_kwargs(local_cwd)
    snippets: list[str] = []
    if sync_overlap and remote_head:
        committed = _run_git_diff(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-color",
                remote_head,
                commit_sha,
                "--",
                _literal_pathspec(path),
            ],
            **cwd_kwargs,
        )
        if committed:
            snippets.append(
                f"# local committed diff ({remote_head[:7]}..{commit_sha[:7]})\n{committed}"
            )
    if dirty_overlap:
        staged = _run_git_diff(
            [
                "git",
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-color",
                "--",
                _literal_pathspec(path),
            ],
            **cwd_kwargs,
        )
        unstaged = _run_git_diff(
            ["git", "diff", "--no-ext-diff", "--no-color", "--", _literal_pathspec(path)],
            **cwd_kwargs,
        )
        if staged:
            snippets.append(f"# local staged diff\n{staged}")
        if unstaged:
            snippets.append(f"# local unstaged diff\n{unstaged}")
    return _cap_diff("\n\n".join(snippets)) if snippets else ""


def _attach_overlap_diffs(
    audit: dict[str, Any],
    *,
    commit_sha: str,
    sync_overlaps: list[str],
    local_overlaps: list[str],
    remote_diffs: dict[str, str] | None = None,
    local_cwd: str | None = None,
) -> None:
    paths = list(dict.fromkeys([*sync_overlaps, *local_overlaps]))[:_MAX_OVERLAP_DIFF_FILES]
    remote_diffs = remote_diffs or {}
    overlap_diffs: dict[str, dict[str, str]] = {}
    for path in paths:
        remote_diff = _cap_diff(str(remote_diffs.get(path) or ""))
        local_diff = _local_overlap_diff(
            path=path,
            remote_head=audit.get("remote_head"),
            commit_sha=commit_sha,
            sync_overlap=path in sync_overlaps,
            dirty_overlap=path in local_overlaps,
            local_cwd=local_cwd,
        )
        overlap_diffs[path] = {
            "remote": remote_diff,
            "local": local_diff,
        }
    audit["overlap_diffs"] = overlap_diffs


def _remote_dirty_file_matches_commit(
    audit: dict[str, Any],
    *,
    path: str,
    commit_sha: str,
    local_cwd: str | None = None,
) -> bool:
    dirty_status = next(
        (
            str(item.get("status") or "")
            for item in list(audit.get("dirty_files") or [])
            if item.get("path") == path
        ),
        "",
    )
    required_hashes: list[str] = []
    if dirty_status[:1] not in {"", " ", "?", "U"}:
        required_hashes.append("index")
    if dirty_status[1:2] not in {"", " ", "?", "U"}:
        required_hashes.append("worktree")
    if not required_hashes:
        return False

    remote_hashes_by_path = dict(audit.get("remote_dirty_hashes") or {})
    raw_remote_hashes = remote_hashes_by_path.get(path) or {}
    if isinstance(raw_remote_hashes, str):
        raw_remote_hashes = {"worktree": raw_remote_hashes}
    remote_hashes = dict(raw_remote_hashes)
    local_hash = _local_blob_hash(commit_sha, path, **_git_cwd_kwargs(local_cwd))
    if local_hash is None:
        return False
    return all(
        str(remote_hashes.get(hash_kind) or "").strip().lower() == local_hash
        for hash_kind in required_hashes
    )


def _make_remote_diff_loader(
    *,
    target_dir: str,
    bridge_name: str | None,
    tunnel_config: TunnelConfig | None,
    timeout: int,
    run_command_fn: RemoteCommandRunner | None = None,
) -> Callable[[list[str]], dict[str, str]]:
    def _load(paths: list[str]) -> dict[str, str]:
        kwargs: dict[str, Any] = {
            "bridge_name": bridge_name,
            "config": tunnel_config,
            "timeout": timeout,
            "max_lines": _MAX_DIFF_LINES,
        }
        if run_command_fn is not None:
            kwargs["run_command_fn"] = run_command_fn
        return collect_remote_sync_diffs(target_dir, paths, **kwargs)

    return _load


def _apply_remote_audit_policy(
    audit: dict[str, Any],
    *,
    commit_sha: str,
    local_dirty_paths: set[str] | None = None,
    remote_diff_loader: Callable[[list[str]], dict[str, str]] | None = None,
    local_cwd: str | None = None,
) -> dict[str, Any]:
    """Attach sync safety decisions to a raw remote audit."""
    audit = dict(audit)
    dirty_files = list(audit.get("dirty_files") or [])
    large_files = list(audit.get("untracked_large_files") or [])
    blocking_reasons: list[str] = []

    if not audit.get("probe_success", True):
        blocking_reasons.append(f"remote audit failed: {audit.get('error') or 'unknown error'}")
    elif not audit.get("target_exists", False):
        blocking_reasons.append("remote target directory does not exist")
    elif not audit.get("is_git_repo", False):
        blocking_reasons.append("remote target directory is not a git repository")
    else:
        if audit.get("merge_in_progress"):
            blocking_reasons.append("remote repository has a merge in progress")
        if audit.get("rebase_in_progress"):
            blocking_reasons.append("remote repository has a rebase in progress")
        if dirty_files:
            remote_dirty_paths = {str(item.get("path") or "") for item in dirty_files}
            incoming_paths = _local_changed_paths(
                audit.get("remote_head"), commit_sha, **_git_cwd_kwargs(local_cwd)
            )
            sync_overlaps = sorted(remote_dirty_paths & incoming_paths)
            local_overlaps = sorted(remote_dirty_paths & (local_dirty_paths or set()))
            matching_commit_paths = sorted(
                path
                for path in sync_overlaps
                if _remote_dirty_file_matches_commit(
                    audit, path=path, commit_sha=commit_sha, local_cwd=local_cwd
                )
            )
            divergent_sync_overlaps = [
                path for path in sync_overlaps if path not in set(matching_commit_paths)
            ]
            audit["remote_local_sync_overlaps"] = divergent_sync_overlaps
            audit["remote_local_dirty_overlaps"] = local_overlaps
            audit["remote_dirty_matches_local_commit"] = matching_commit_paths
            if divergent_sync_overlaps or local_overlaps:
                diff_paths = list(dict.fromkeys([*divergent_sync_overlaps, *local_overlaps]))[
                    :_MAX_OVERLAP_DIFF_FILES
                ]
                remote_diffs = remote_diff_loader(diff_paths) if remote_diff_loader else {}
                _attach_overlap_diffs(
                    audit,
                    commit_sha=commit_sha,
                    sync_overlaps=divergent_sync_overlaps,
                    local_overlaps=local_overlaps,
                    remote_diffs=remote_diffs,
                    local_cwd=local_cwd,
                )
                blocking_reasons.append("remote dirty files overlap local changes")
            elif matching_commit_paths:
                blocking_reasons.append("remote dirty files already match the local commit")
            else:
                blocking_reasons.append("remote worktree has tracked or staged changes")
        else:
            audit["remote_local_sync_overlaps"] = []
            audit["remote_local_dirty_overlaps"] = []
            audit["remote_dirty_matches_local_commit"] = []
            audit["overlap_diffs"] = {}

    audit["blocking_reasons"] = blocking_reasons
    audit["safe_to_sync"] = not blocking_reasons
    audit["large_untracked_count"] = len(large_files)
    return audit


def _render_remote_audit_lines(audit: dict[str, Any] | None) -> list[str]:
    if not audit:
        return []

    lines = [
        "Remote audit:",
        f"  Bridge: {audit.get('bridge_name') or '(default)'}",
        f"  Target: {audit.get('target_dir') or '(unknown)'}",
        f"  Remote HEAD: {audit.get('remote_head') or '(unknown)'}",
    ]

    blocking_reasons = list(audit.get("blocking_reasons") or [])
    if blocking_reasons:
        lines.append("  Blocking reasons:")
        lines.extend(f"    - {reason}" for reason in blocking_reasons[:5])
        if len(blocking_reasons) > 5:
            lines.append(f"    - ... {len(blocking_reasons) - 5} more")
    elif audit.get("safe_to_sync"):
        lines.append("  Safety: safe to sync")

    dirty_files = list(audit.get("dirty_files") or [])
    if dirty_files:
        sync_overlaps = set(audit.get("remote_local_sync_overlaps") or [])
        dirty_overlaps = set(audit.get("remote_local_dirty_overlaps") or [])
        matching_commit = set(audit.get("remote_dirty_matches_local_commit") or [])
        lines.append("  Dirty files:")
        for item in dirty_files[:10]:
            status = str(item.get("status") or "??").strip() or "??"
            path = item.get("path") or "(unknown)"
            if path in sync_overlaps or path in dirty_overlaps:
                marker = " (overlaps local)"
            elif path in matching_commit:
                marker = " (matches local commit)"
            else:
                marker = ""
            lines.append(f"    - {status} {path}{marker}")
        if len(dirty_files) > 10:
            lines.append(f"    - ... {len(dirty_files) - 10} more")

    large_files = list(audit.get("untracked_large_files") or [])
    if large_files:
        threshold = _human_size(audit.get("large_file_threshold_bytes"))
        lines.append(f"  Large untracked files >= {threshold}:")
        for item in large_files[:10]:
            path = item.get("path") or "(unknown)"
            lines.append(f"    - {path} ({_human_size(item.get('size'))})")
        if len(large_files) > 10:
            lines.append(f"    - ... {len(large_files) - 10} more")

    if audit.get("scan_complete") and not audit.get("large_file_scan_complete", True):
        skipped = list(audit.get("large_file_scan_skipped_patterns") or [])
        lines.append("  Large file scan: incomplete (patterns below are never scanned)")
        if skipped:
            lines.append(f"    {', '.join(skipped)}")

    overlap_diffs = dict(audit.get("overlap_diffs") or {})
    if overlap_diffs:
        lines.append("  Overlap diffs:")
        for path, diffs in overlap_diffs.items():
            lines.append(f"    File: {path}")
            remote_diff = str((diffs or {}).get("remote") or "").strip()
            if remote_diff:
                lines.append("    Remote dirty diff:")
                lines.extend(f"      {line}" for line in remote_diff.splitlines())
            local_diff = str((diffs or {}).get("local") or "").strip()
            if local_diff:
                lines.append("    Local diff:")
                lines.extend(f"      {line}" for line in local_diff.splitlines())

    return lines


def _emit_sync_error(
    ctx: Context,
    *,
    error_type: str,
    message: str,
    exit_code: int,
    hint: str | None = None,
    details: str | None = None,
    remote_audit: dict[str, Any] | None = None,
) -> None:
    if ctx.json_output:
        error_data: dict[str, Any] = {
            "type": error_type,
            "message": message,
            "code": exit_code,
        }
        if hint:
            error_data["hint"] = hint
        if remote_audit:
            error_data["remote_audit"] = remote_audit
        click.echo(
            json.dumps({"success": False, "error": error_data}, indent=2, ensure_ascii=False),
            err=True,
        )
        return

    human_lines = _render_remote_audit_lines(remote_audit)
    if ctx.debug and details and details != message.removeprefix("Sync failed: "):
        human_lines.extend(["Details:", details])
    emit_error(
        ctx,
        error_type=error_type,
        message=message,
        exit_code=exit_code,
        hint=hint,
        human_lines=human_lines if human_lines else None,
    )


def sync_via_tunnel(
    ctx: Context,
    config: Config,
    *,
    branch: str,
    commit_sha: str,
    commit_msg: str,
    remote: str,
    timeout: int,
    force: bool = False,
    offline_bundle: bool = False,
    bridge_name: Optional[str] = None,
    fallback_bridge_names: Optional[list[str]] = None,
    tunnel_config=None,
    remote_audit: dict[str, Any] | None = None,
    large_file_threshold_bytes: int = _DEFAULT_LARGE_FILE_THRESHOLD_MB * _BYTES_PER_MIB,
    local_dirty_paths: set[str] | None = None,
    transport_name: str = "ssh",
    run_command_fn: RemoteCommandRunner | None = None,
    transfer_fn: FileTransferRunner | None = None,
    target_dir: str | None = None,
    local_cwd: str | None = None,
) -> int:
    """Sync code through the selected Bridge command/transfer transport."""
    effective_target_dir = target_dir if target_dir is not None else config.target_dir
    assert effective_target_dir is not None
    initial_bridge_name = bridge_name
    bridge_sequence: list[str | None] = [initial_bridge_name] if initial_bridge_name else [None]
    if fallback_bridge_names:
        bridge_sequence.extend(fallback_bridge_names)

    result: dict[str, object] = {"success": False, "synced_sha": None, "error": "Unknown error"}
    last_bridge_name = bridge_sequence[0]
    last_remote_audit = remote_audit

    for index, current_bridge_name in enumerate(bridge_sequence):
        last_bridge_name = current_bridge_name
        if index == 0 and remote_audit is not None:
            attempt_audit = remote_audit
        else:
            audit_kwargs: dict[str, Any] = {
                "target_dir": effective_target_dir,
                "branch": branch,
                "commit_sha": commit_sha,
                "large_file_threshold_bytes": large_file_threshold_bytes,
                "bridge_name": current_bridge_name,
                "config": tunnel_config,
                "timeout": timeout,
            }
            if run_command_fn is not None:
                audit_kwargs["run_command_fn"] = run_command_fn
            attempt_audit = _apply_remote_audit_policy(
                audit_remote_sync_state(**audit_kwargs),
                commit_sha=commit_sha,
                local_dirty_paths=local_dirty_paths,
                local_cwd=local_cwd,
                remote_diff_loader=_make_remote_diff_loader(
                    target_dir=effective_target_dir,
                    bridge_name=current_bridge_name,
                    tunnel_config=tunnel_config,
                    timeout=timeout,
                    run_command_fn=run_command_fn,
                ),
            )
        last_remote_audit = attempt_audit
        if not attempt_audit.get("safe_to_sync"):
            result = {
                "success": False,
                "synced_sha": None,
                "error": "Remote worktree is not safe to sync",
            }
            break

        if ctx.debug and not ctx.json_output:
            if current_bridge_name:
                emit_progress(
                    ctx,
                    f"Syncing via {transport_name} (bridge: {current_bridge_name})...",
                )
            else:
                emit_progress(ctx, f"Syncing via {transport_name}...")

        if offline_bundle:
            sync_kwargs: dict[str, Any] = {}
            if run_command_fn is not None:
                sync_kwargs["run_command_fn"] = run_command_fn
            if transfer_fn is not None:
                sync_kwargs["transfer_fn"] = transfer_fn
            if local_cwd is not None:
                sync_kwargs["local_cwd"] = local_cwd
            result = sync_via_ssh_bundle(
                target_dir=effective_target_dir,
                branch=branch,
                commit_sha=commit_sha,
                force=force,
                bridge_name=current_bridge_name,
                config=tunnel_config,
                timeout=timeout,
                **sync_kwargs,
            )
        else:
            sync_kwargs = {}
            if run_command_fn is not None:
                sync_kwargs["run_command_fn"] = run_command_fn
            result = sync_via_ssh(
                target_dir=effective_target_dir,
                branch=branch,
                commit_sha=commit_sha,
                remote=remote,
                force=force,
                bridge_name=current_bridge_name,
                config=tunnel_config,
                timeout=timeout,
                **sync_kwargs,
            )

        if result.get("success"):
            break

        has_next_bridge = index < len(bridge_sequence) - 1
        if not has_next_bridge or not _is_sync_timeout_error(result.get("error")):
            break

        next_bridge_name = bridge_sequence[index + 1]
        if not ctx.json_output:
            from_bridge = current_bridge_name or "<default>"
            to_bridge = next_bridge_name or "<default>"
            emit_warning(
                ctx,
                f"sync timed out on bridge '{from_bridge}'; retrying with '{to_bridge}'.",
            )

    if result.get("success"):
        if (
            last_bridge_name
            and tunnel_config is not None
            and last_bridge_name != initial_bridge_name
            and tunnel_config.default_bridge != last_bridge_name
        ):
            tunnel_config.default_bridge = last_bridge_name
            try:
                save_tunnel_config(tunnel_config)
            except Exception as e:
                if not ctx.json_output:
                    emit_warning(
                        ctx,
                        f"sync succeeded but failed to persist updated default bridge '{last_bridge_name}': {e}",
                    )
        synced_sha = str(result.get("synced_sha") or commit_sha[:7])
        bundle_mode = result.get("bundle_mode") if offline_bundle else None
        bundle_base_sha = result.get("bundle_base_sha") if offline_bundle else None
        payload: dict[str, object] = {
            "status": "success",
            "method": (f"{transport_name}_bundle" if offline_bundle else transport_name),
            "branch": branch,
            "remote": remote,
            "commit": commit_sha[:7],
            "commit_full": commit_sha,
            "synced_sha": synced_sha,
            "message": commit_msg,
            "target_dir": effective_target_dir,
        }
        if last_bridge_name:
            payload["bridge_name"] = last_bridge_name
        if bundle_mode:
            payload["bundle_mode"] = bundle_mode
        if bundle_base_sha:
            payload["bundle_base_sha"] = bundle_base_sha

        if ctx.debug and not ctx.json_output:
            emit_info(ctx, f"Synced branch '{branch}' ({synced_sha[:7]}) to {effective_target_dir}")
            emit_info(ctx, f"  Commit: {commit_msg}")
            if last_bridge_name:
                emit_info(ctx, f"  Bridge: {last_bridge_name}")
            if offline_bundle:
                mode_suffix = f", {bundle_mode}" if bundle_mode else ""
                emit_info(ctx, f"  Method: {transport_name} (offline bundle{mode_suffix})")
            else:
                emit_info(ctx, f"  Method: {transport_name}")
        else:
            emit_success(
                ctx,
                payload=payload,
                text=(
                    f"synced {synced_sha[:7]} via "
                    f"{transport_name}{'-bundle' if offline_bundle else ''}"
                ),
            )
        return EXIT_SUCCESS

    message, hint, details = _summarize_sync_failure(
        raw_error=result.get("error"),
        branch=branch,
        remote=remote,
    )

    _emit_sync_error(
        ctx,
        error_type="SyncError",
        message=f"Sync failed: {message}",
        exit_code=EXIT_GENERAL_ERROR,
        hint=hint,
        details=details,
        remote_audit=last_remote_audit,
    )
    return EXIT_GENERAL_ERROR


@click.command()
@click.option(
    "bridge_name",
    "--bridge",
    "-b",
    default=None,
    help="Use a specific configured bridge instead of automatic bridge selection",
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
    help="Skip git push before sync (default behavior; same as --push-mode skip)",
)
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Allow sync with uncommitted changes (syncs committed branch tip only)",
)
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Force sync: implies --allow-dirty, hard-resets the Bridge branch to the selected "
        "commit and discards uncommitted work on Bridge. The flag itself is the confirmation; "
        "no extra prompt follows"
    ),
)
@click.option(
    "--timeout",
    default=120,
    help="Timeout in seconds when waiting for sync (default: 120)",
)
@click.option(
    "--source",
    type=click.Choice(["remote", "bundle"], case_sensitive=False),
    default="bundle",
    show_default=True,
    help="Choose remote git fetch or local bundle transfer (bundle is recommended)",
)
@click.option(
    "transport_name",
    "--transport",
    type=click.Choice(BRIDGE_TRANSPORTS, case_sensitive=False),
    default="auto",
    show_default=True,
    help="Sync transport: prefer healthy SSH, otherwise use notebook Jupyter",
)
@click.option(
    "--push-mode",
    type=click.Choice(["required", "best-effort", "skip"], case_sensitive=False),
    default=None,
    help="Git push policy before sync (default: skip; use required/best-effort to enable push)",
)
@click.option(
    "--pull",
    is_flag=True,
    help="Pull changes from Bridge before syncing (bidirectional sync)",
)
@click.option(
    "--pull-strategy",
    type=click.Choice(["merge", "rebase"], case_sensitive=False),
    default="merge",
    show_default=True,
    help="Strategy for integrating Bridge changes when --pull is used",
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Audit the selected Bridge worktree for sync safety without changing anything",
)
@click.option(
    "--large-file-threshold-mb",
    default=_DEFAULT_LARGE_FILE_THRESHOLD_MB,
    show_default=True,
    type=click.IntRange(min=0),
    help="Report untracked remote files at or above this size during sync safety audit",
)
@click.option(
    "--dest",
    "dest",
    default=None,
    help="Override the remote target directory instead of the configured target_dir",
)
@click.option(
    "local_path",
    "--local",
    default=None,
    help="Run git operations against this local repository root instead of the current directory",
)
@json_option
@pass_context
def sync(
    ctx: Context,
    bridge_name: Optional[str],
    remote: Optional[str],
    no_push: bool,
    allow_dirty: bool,
    force: bool,
    timeout: int,
    source: str,
    transport_name: str,
    push_mode: Optional[str],
    pull: bool,
    pull_strategy: str,
    check_only: bool,
    large_file_threshold_mb: int,
    dest: Optional[str],
    local_path: Optional[str],
    json_output: bool = False,
) -> None:
    """Sync local code to the Bridge shared filesystem.

    This command syncs through a healthy SSH bridge, or through the notebook's
    Jupyter channel when explicitly selected or when an H100/H200 bridge has no
    reachable SSH ingress.
    Git push is skipped by default unless --push-mode is set:
    - remote: fetch from the git remote over the selected live bridge
    - bundle: upload a local git bundle over the selected live bridge (default)

    Use --pull for bidirectional sync when someone has committed changes on the
    Bridge that are not in your local repository.

    \b
    Examples:
        inspire sync                          # Sync current branch via SSH tunnel
        inspire sync --remote upstream        # Sync via upstream remote
        inspire sync --source bundle          # Force local bundle sync over SSH
        inspire sync -b gpu-main --transport jupyter
                                             # Sync a specific GPU notebook without SSH ingress
        inspire sync --push-mode required     # Push before sync (fail if push fails)
        inspire sync --push-mode best-effort  # Continue even if git push fails
        inspire sync --no-push                # Explicitly skip git push (default)
        inspire sync --allow-dirty            # Sync committed branch tip even if worktree is dirty
        inspire sync --check                  # Audit Bridge worktree without syncing
        inspire sync --pull                   # Pull Bridge changes first, then sync
        inspire sync --pull --pull-strategy rebase  # Use rebase instead of merge
        inspire sync --allow-dirty --no-push --source bundle --force
                                             # Hard-reset Bridge to the selected commit,
                                             # discarding uncommitted Bridge work (--force is
                                             # the confirmation; no second prompt follows)
        inspire sync --dest /workspace/other  # Sync into a path other than the configured target_dir
        inspire sync --local ~/repos/other    # Read branch/commit from another git checkout

    \b
    Environment variables:
        INSPIRE_DEFAULT_REMOTE    Default git remote (default: origin)
        INSPIRE_TARGET_DIR        Target directory on Bridge (required unless --dest is given)

    \b
    --local only changes where local git commands run; configuration is still read
    from the current working directory.
    """

    json_output = resolve_json_output(ctx, json_output)
    local_cwd = _resolve_local_repo_root(ctx, local_path)
    git_kwargs = _git_cwd_kwargs(local_cwd)
    try:
        config, _ = Config.from_files_and_env(
            require_target_dir=dest is None,
            require_credentials=False,
        )
    except ConfigError as e:
        emit_error(
            ctx,
            error_type="ConfigError",
            message=f"Configuration error: {e}",
            exit_code=EXIT_CONFIG_ERROR,
        )
        sys.exit(EXIT_CONFIG_ERROR)

    target_dir = dest if dest is not None else config.target_dir
    assert target_dir is not None

    # Determine current branch
    branch = get_current_branch(**git_kwargs)

    # Determine remote
    if remote is None:
        remote = config.default_remote

    source = source.lower().strip()
    transport_name = transport_name.lower().strip()
    push_mode = push_mode.lower().strip() if push_mode else None

    if no_push and push_mode and push_mode != "skip":
        emit_error(
            ctx,
            error_type="ValidationError",
            message="--no-push conflicts with --push-mode values other than 'skip'",
            exit_code=EXIT_CONFIG_ERROR,
        )
        sys.exit(EXIT_CONFIG_ERROR)

    tunnel_config = None
    selected_bridge = None
    use_offline_bundle = False
    candidate_bridges: list[BridgeProfile] = []
    fallback_bridge_names: list[str] = []
    tunnel_config = load_tunnel_config()
    candidate_bridges = _ordered_bridges_for_sync(tunnel_config)
    if bridge_name:
        requested_bridge = tunnel_config.get_bridge(bridge_name)
        if requested_bridge is None:
            emit_error(
                ctx,
                error_type="BridgeNotFound",
                message=f"Bridge '{bridge_name}' not found.",
                exit_code=EXIT_CONFIG_ERROR,
                hint="Run 'inspire tunnel list' to inspect configured bridge names.",
            )
            sys.exit(EXIT_CONFIG_ERROR)
        candidate_bridges = [requested_bridge]
    if not candidate_bridges:
        emit_error(
            ctx,
            error_type="TunnelUnavailable",
            message="No bridge configured for SSH sync",
            exit_code=EXIT_CONFIG_ERROR,
            hint="Use 'inspire tunnel list' or 'inspire notebook ssh <id>' first.",
        )
        sys.exit(EXIT_CONFIG_ERROR)

    selected_transport = "ssh"
    tried_bridges: list[str] = []
    if transport_name == "jupyter":
        live_bridges = [
            bridge for bridge in candidate_bridges if str(bridge.notebook_id or "").strip()
        ]
    else:
        ssh_candidates = [
            bridge for bridge in candidate_bridges if str(bridge.proxy_url or "").strip()
        ]
        live_bridges, tried_bridges = _probe_live_bridges(
            tunnel_config=tunnel_config,
            config=config,
            candidate_bridges=ssh_candidates,
        )
        if not live_bridges and transport_name == "auto":
            live_bridges = [
                bridge
                for bridge in candidate_bridges
                if not bridge.has_internet and str(bridge.notebook_id or "").strip()
            ]
            if live_bridges:
                selected_transport = "jupyter"

    if not live_bridges:
        tried_csv = ", ".join(tried_bridges or [bridge.name for bridge in candidate_bridges])
        if transport_name == "jupyter":
            message = "No configured bridge has a notebook_id binding for Jupyter sync"
            hint = "Re-create a notebook bridge so its profile records notebook_id."
        else:
            message = f"SSH tunnel is not available for any configured bridge (tried: {tried_csv})"
            hint = (
                "Run 'inspire tunnel status' to troubleshoot, or use "
                "'--transport jupyter' for a notebook-backed bridge."
            )
        emit_error(
            ctx,
            error_type="TunnelUnavailable",
            message=message,
            exit_code=EXIT_GENERAL_ERROR,
            hint=hint,
        )
        sys.exit(EXIT_GENERAL_ERROR)

    if transport_name == "jupyter":
        selected_transport = "jupyter"

    eligible_bridges = _eligible_live_bridges(source=source, live_bridges=live_bridges)
    if not eligible_bridges and source == "remote":
        first_live = live_bridges[0]
        emit_error(
            ctx,
            error_type="ValidationError",
            message=f"Bridge '{first_live.name}' has no internet; remote source is unavailable",
            exit_code=EXIT_CONFIG_ERROR,
            hint="Use '--source bundle' for no-internet bridges.",
        )
        sys.exit(EXIT_CONFIG_ERROR)

    selected_bridge = eligible_bridges[0]
    fallback_bridge_names = (
        [bridge.name for bridge in eligible_bridges[1:]] if selected_transport == "ssh" else []
    )
    use_offline_bundle = source == "bundle"
    run_command_fn: RemoteCommandRunner | None = None
    transfer_fn: FileTransferRunner | None = None
    if selected_transport == "jupyter":
        session = require_web_session(
            ctx,
            hint="Jupyter sync requires web authentication for the selected account.",
        )
        client = JupyterBridgeClient(
            notebook_id=str(selected_bridge.notebook_id),
            session=session,
        )
        run_command_fn = client.run_command
        transfer_fn = client.transfer

    if ctx.debug and not ctx.json_output:
        emit_info(ctx, f"Using bridge '{selected_bridge.name}' for sync.")
        emit_info(ctx, f"Using {selected_transport} transport.")
        if use_offline_bundle:
            emit_info(ctx, "Using offline bundle sync path (--source bundle).")
        else:
            emit_info(ctx, "Using remote git sync path (--source remote).")

    commit_sha = get_current_commit_sha(branch, **git_kwargs)
    commit_msg = get_commit_message(branch, **git_kwargs)
    large_file_threshold_bytes = large_file_threshold_mb * _BYTES_PER_MIB
    local_dirty_paths = _local_dirty_paths(**git_kwargs)
    local_is_dirty = bool(local_dirty_paths) or has_uncommitted_changes(**git_kwargs)

    audit_kwargs: dict[str, Any] = {
        "target_dir": target_dir,
        "branch": branch,
        "commit_sha": commit_sha,
        "large_file_threshold_bytes": large_file_threshold_bytes,
        "bridge_name": selected_bridge.name,
        "config": tunnel_config,
        "timeout": timeout,
    }
    if run_command_fn is not None:
        audit_kwargs["run_command_fn"] = run_command_fn
    remote_audit = _apply_remote_audit_policy(
        audit_remote_sync_state(**audit_kwargs),
        commit_sha=commit_sha,
        local_dirty_paths=local_dirty_paths,
        local_cwd=local_cwd,
        remote_diff_loader=_make_remote_diff_loader(
            target_dir=target_dir,
            bridge_name=selected_bridge.name,
            tunnel_config=tunnel_config,
            timeout=timeout,
            run_command_fn=run_command_fn,
        ),
    )

    if check_only:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    remote_audit,
                    success=bool(remote_audit.get("safe_to_sync")),
                )
            )
        else:
            for line in _render_remote_audit_lines(remote_audit):
                click.echo(line)
        sys.exit(EXIT_SUCCESS if remote_audit.get("safe_to_sync") else EXIT_GENERAL_ERROR)

    if local_is_dirty:
        if not (allow_dirty or force):
            hint = (
                "Commit/stash changes, or pass --allow-dirty to sync committed HEAD only. "
                "Use --force only when you also want force-sync behavior "
                "(implies --allow-dirty and may overwrite a diverged Bridge branch)."
            )
            show_remote_audit = bool(
                remote_audit.get("remote_local_dirty_overlaps")
                or remote_audit.get("remote_local_sync_overlaps")
                or not remote_audit.get("safe_to_sync")
            )
            _emit_sync_error(
                ctx,
                error_type="ValidationError",
                message="Uncommitted changes detected",
                exit_code=EXIT_GENERAL_ERROR,
                hint=hint,
                remote_audit=remote_audit if show_remote_audit else None,
            )
            sys.exit(EXIT_GENERAL_ERROR)

        dirty_override_flag = "--allow-dirty" if allow_dirty else "--force"
        if not ctx.json_output:
            emit_warning(
                ctx,
                f"Uncommitted changes detected; syncing committed tip of '{branch}' only ({dirty_override_flag}).",
            )

    if not remote_audit.get("safe_to_sync"):
        _emit_sync_error(
            ctx,
            error_type="RemoteWorktreeDirty",
            message="Remote worktree is not safe to sync",
            exit_code=EXIT_GENERAL_ERROR,
            hint=(
                "Resolve the Bridge-side blockers, or inspect them with "
                "'inspire --json sync --check'."
            ),
            remote_audit=remote_audit,
        )
        sys.exit(EXIT_GENERAL_ERROR)

    # Handle --pull: pull changes from Bridge before syncing
    if pull:
        if not ctx.json_output:
            emit_info(ctx, f"Checking for changes on Bridge '{selected_bridge.name}'...")

        pull_kwargs: dict[str, Any] = {}
        if run_command_fn is not None:
            pull_kwargs["run_command_fn"] = run_command_fn
        if transfer_fn is not None:
            pull_kwargs["transfer_fn"] = transfer_fn
        pull_result = pull_from_bridge(
            target_dir=target_dir,
            branch=branch,
            local_sha=commit_sha,
            bridge_name=selected_bridge.name,
            config=tunnel_config,
            timeout=timeout,
            strategy=pull_strategy,
            local_cwd=local_cwd,
            **pull_kwargs,
        )

        if pull_result.get("success"):
            pulled_sha = pull_result.get("pulled_sha")
            if pulled_sha and pulled_sha != commit_sha:
                commit_sha = pulled_sha
                commit_msg = get_commit_message(branch, **git_kwargs)
                if not ctx.json_output:
                    emit_success(
                        ctx,
                        payload=pull_result,
                        text=pull_result.get("message", "Pulled Bridge changes"),
                    )
            elif not ctx.json_output:
                emit_info(ctx, pull_result.get("message", "No Bridge changes to pull"))
        else:
            error_msg = pull_result.get("error", "Unknown error")
            if "in progress" in error_msg.lower():
                hint = (
                    "Finish the existing merge/rebase first, or abort it explicitly in the local "
                    "repository; sync will not abort it for you."
                )
            elif "merge" in error_msg.lower() or "rebase" in error_msg.lower():
                hint = (
                    "Merge conflicts detected. Resolve conflicts and commit, "
                    "or run 'git merge --abort' / 'git rebase --abort' to cancel."
                )
            else:
                hint = (
                    "Check Bridge status and retry. "
                    "You can skip --pull and force-sync with --force if needed."
                )
            emit_error(
                ctx,
                error_type="PullError",
                message=f"Failed to pull from Bridge: {error_msg}",
                exit_code=EXIT_GENERAL_ERROR,
                hint=hint,
            )
            sys.exit(EXIT_GENERAL_ERROR)

    effective_push_mode = _effective_push_mode(
        no_push=no_push,
        push_mode=push_mode,
    )

    if effective_push_mode != "skip":
        try:
            push_to_remote(
                branch,
                remote,
                show_progress=ctx.debug and not ctx.json_output,
                **_git_cwd_kwargs(local_cwd),
            )
        except click.ClickException as e:
            if effective_push_mode == "best-effort":
                if not ctx.json_output:
                    emit_warning(
                        ctx,
                        f"{e}. Continuing because push mode is best-effort.",
                    )
            else:
                if ctx.json_output:
                    emit_error(
                        ctx,
                        error_type="GitError",
                        message=str(e),
                        exit_code=EXIT_GENERAL_ERROR,
                    )
                    sys.exit(EXIT_GENERAL_ERROR)
                raise
    elif ctx.debug and not ctx.json_output:
        emit_info(ctx, "Skipping git push before sync.")

    exit_code = sync_via_tunnel(
        ctx,
        config,
        branch=branch,
        commit_sha=commit_sha,
        commit_msg=commit_msg,
        remote=remote,
        timeout=timeout,
        force=force,
        offline_bundle=use_offline_bundle,
        bridge_name=selected_bridge.name,
        fallback_bridge_names=fallback_bridge_names,
        tunnel_config=tunnel_config,
        remote_audit=remote_audit,
        large_file_threshold_bytes=large_file_threshold_bytes,
        local_dirty_paths=local_dirty_paths,
        transport_name=selected_transport,
        run_command_fn=run_command_fn,
        transfer_fn=transfer_fn,
        target_dir=target_dir,
        local_cwd=local_cwd,
    )
    sys.exit(exit_code)
