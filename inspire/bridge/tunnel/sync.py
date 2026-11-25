"""Sync helpers implemented over SSH tunnel access."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from .config import load_tunnel_config
from .models import TunnelConfig, TunnelError
from .scp import run_scp_transfer
from .ssh_exec import run_ssh_command

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
logger = logging.getLogger(__name__)

_AUDIT_PREFIX = "__INSPIRE_SYNC_AUDIT__"

#: Directories the remote large-file scan never descends into: they are where
#: virtualenvs and training artifacts live, so scanning them would make every
#: sync report gigabytes of expected files. The audit surfaces them as skipped
#: patterns so ``scan_complete`` (the probe ran) is never read as "every file
#: was checked".
_LARGE_FILE_SCAN_EXCLUDED_PATTERNS: tuple[str, ...] = (
    ".venv/**",
    ".cache/**",
    "third_party/**",
    "data/**",
    "checkpoints/**",
    "logs/**",
    "**/.venv/**",
    "**/.cache/**",
    "**/third_party/**",
    "**/data/**",
    "**/checkpoints/**",
    "**/logs/**",
)


RemoteCommandRunner = Callable[..., subprocess.CompletedProcess[str]]
FileTransferRunner = Callable[..., subprocess.CompletedProcess[str]]


def _parse_bool(value: str) -> bool:
    return value == "1" or value.lower() == "true"


def _parse_audit_output(
    stdout: str,
    *,
    target_dir: str,
    branch: str,
    bridge_name: Optional[str],
    large_file_threshold_bytes: int,
) -> dict[str, Any]:
    """Parse the NUL-delimited remote sync audit protocol."""
    audit: dict[str, Any] = {
        "probe_success": True,
        "bridge_name": bridge_name,
        "target_dir": target_dir,
        "branch": branch,
        "remote_branch": None,
        "remote_head": None,
        "target_exists": False,
        "is_git_repo": False,
        "merge_in_progress": False,
        "rebase_in_progress": False,
        "dirty_files": [],
        "remote_dirty_hashes": {},
        "untracked_large_files": [],
        "large_file_threshold_bytes": large_file_threshold_bytes,
        "large_file_scan_complete": False,
        "large_file_scan_skipped_patterns": list(_LARGE_FILE_SCAN_EXCLUDED_PATTERNS),
        "scan_complete": True,
        "error": None,
    }

    path_keys = {"DIRTY", "DIRTY_INDEX_HASH", "DIRTY_WORKTREE_HASH", "LARGE"}
    tokens = iter((stdout or "").split("\0"))
    for token in tokens:
        if token != _AUDIT_PREFIX:
            continue
        key = next(tokens, "")
        value = next(tokens, "")
        if not key:
            continue
        path = next(tokens, "") if key in path_keys else ""

        if key == "TARGET_EXISTS":
            audit["target_exists"] = _parse_bool(value)
        elif key == "IS_GIT_REPO":
            audit["is_git_repo"] = _parse_bool(value)
        elif key == "REMOTE_HEAD":
            audit["remote_head"] = value or None
        elif key == "REMOTE_BRANCH":
            audit["remote_branch"] = value or None
        elif key == "MERGE_IN_PROGRESS":
            audit["merge_in_progress"] = _parse_bool(value)
        elif key == "REBASE_IN_PROGRESS":
            audit["rebase_in_progress"] = _parse_bool(value)
        elif key == "DIRTY":
            audit["dirty_files"].append({"status": value, "path": path})
        elif key in {"DIRTY_INDEX_HASH", "DIRTY_WORKTREE_HASH"} and path:
            hash_kind = "index" if key == "DIRTY_INDEX_HASH" else "worktree"
            audit["remote_dirty_hashes"].setdefault(path, {})[hash_kind] = value
        elif key == "LARGE":
            try:
                size = int(value)
            except ValueError:
                size = 0
            audit["untracked_large_files"].append({"size": size, "path": path})
        elif key == "ERROR":
            audit["error"] = value

    return audit


def _build_remote_sync_metadata_command(
    target_dir: str,
) -> str:
    """Build a remote fact-emitter script for cheap sync metadata."""
    q_target_dir = shlex.quote(target_dir)
    q_prefix = shlex.quote(_AUDIT_PREFIX)

    return f"""
set +e
prefix={q_prefix}
target_dir={q_target_dir}
emit() {{ printf '%s\\0%s\\0%s\\0' "$prefix" "$1" "$2"; }}

if [ ! -d "$target_dir" ]; then
  emit TARGET_EXISTS 0
  emit IS_GIT_REPO 0
  exit 0
fi
emit TARGET_EXISTS 1
cd "$target_dir" || {{ emit ERROR "cannot cd to target"; exit 0; }}
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  emit IS_GIT_REPO 0
  exit 0
fi
emit IS_GIT_REPO 1
head_sha="$(git rev-parse HEAD 2>/dev/null || true)"
branch_name="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
emit REMOTE_HEAD "$head_sha"
emit REMOTE_BRANCH "$branch_name"

git_dir="$(git rev-parse --git-dir 2>/dev/null || true)"
if [ -n "$git_dir" ] && [ -f "$git_dir/MERGE_HEAD" ]; then
  emit MERGE_IN_PROGRESS 1
else
  emit MERGE_IN_PROGRESS 0
fi
if [ -n "$git_dir" ] && {{ [ -d "$git_dir/rebase-merge" ] || [ -d "$git_dir/rebase-apply" ]; }}; then
  emit REBASE_IN_PROGRESS 1
else
  emit REBASE_IN_PROGRESS 0
fi
""".strip()


def _build_remote_sync_scan_command(
    target_dir: str,
    large_file_threshold_bytes: int,
) -> str:
    """Build a remote fact-emitter script for dirty and large-file scan."""
    q_target_dir = shlex.quote(target_dir)
    q_threshold = shlex.quote(str(max(0, large_file_threshold_bytes)))
    q_prefix = shlex.quote(_AUDIT_PREFIX)
    exclude_pathspecs = " \\\n  ".join(
        shlex.quote(f":(glob,exclude){pattern}") for pattern in _LARGE_FILE_SCAN_EXCLUDED_PATTERNS
    )

    return f"""
set +e
prefix={q_prefix}
target_dir={q_target_dir}
large_threshold={q_threshold}
emit_path() {{
  printf '%s\\0%s\\0%s\\0%s\\0' "$prefix" "$1" "$2" "$3"
}}

if [ ! -d "$target_dir" ]; then
  exit 0
fi
cd "$target_dir" || exit 0
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 0
fi

git status --porcelain=v1 -z --untracked-files=no 2>/dev/null | while IFS= read -r -d '' entry; do
  status="${{entry:0:2}}"
  path="${{entry:3}}"
  if [ -n "$path" ]; then
    emit_path DIRTY "$status" "$path"
    index_status="${{status:0:1}}"
    worktree_status="${{status:1:1}}"
    case "$index_status" in
      ' '|\\?|U) ;;
      *)
        index_hash="$(git rev-parse ":$path" 2>/dev/null || true)"
        if [ -n "$index_hash" ]; then
          emit_path DIRTY_INDEX_HASH "$index_hash" "$path"
        fi
        ;;
    esac
    case "$worktree_status" in
      ' '|\\?|U) ;;
      *)
        if [ -f "$path" ]; then
          worktree_hash="$(git hash-object -- "$path" 2>/dev/null || true)"
          if [ -n "$worktree_hash" ]; then
            emit_path DIRTY_WORKTREE_HASH "$worktree_hash" "$path"
          fi
        fi
        ;;
    esac
    case "$index_status" in
      R|C)
        IFS= read -r -d '' _old_path || true
        ;;
    esac
  else
    case "${{status:0:1}}" in
      R|C)
        IFS= read -r -d '' _old_path || true
        ;;
    esac
  fi
done

git ls-files -z --others --exclude-standard -- . \\
  {exclude_pathspecs} \\
  2>/dev/null | while IFS= read -r -d '' path; do
  [ -f "$path" ] || continue
  size="$(wc -c < "$path" 2>/dev/null | tr -d ' ' || printf 0)"
  case "$size" in ''|*[!0-9]*) size=0 ;; esac
  if [ "$size" -ge "$large_threshold" ]; then
    emit_path LARGE "$size" "$path"
  fi
done
""".strip()


def _empty_remote_audit(
    *,
    target_dir: str,
    branch: str,
    bridge_name: Optional[str],
    large_file_threshold_bytes: int,
    error: str,
) -> dict[str, Any]:
    return {
        "probe_success": False,
        "bridge_name": bridge_name,
        "target_dir": target_dir,
        "branch": branch,
        "remote_branch": None,
        "remote_head": None,
        "target_exists": False,
        "is_git_repo": False,
        "merge_in_progress": False,
        "rebase_in_progress": False,
        "dirty_files": [],
        "remote_dirty_hashes": {},
        "untracked_large_files": [],
        "large_file_threshold_bytes": large_file_threshold_bytes,
        "large_file_scan_complete": False,
        "large_file_scan_skipped_patterns": list(_LARGE_FILE_SCAN_EXCLUDED_PATTERNS),
        "scan_complete": False,
        "error": error,
    }


def audit_remote_sync_state(
    target_dir: str,
    branch: str,
    commit_sha: str,
    *,
    large_file_threshold_bytes: int,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 60,
    run_command_fn: RemoteCommandRunner | None = None,
) -> dict[str, Any]:
    """Read-only audit of a Bridge worktree before sync mutates it."""
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command

    metadata_timeout = max(5, min(timeout, 15))
    scan_timeout = max(15, timeout)

    try:
        metadata_result = run_command_fn(
            _build_remote_sync_metadata_command(target_dir),
            bridge_name=bridge_name,
            config=config,
            timeout=metadata_timeout,
            capture_output=True,
            check=False,
            pass_stdin=True,
        )
    except subprocess.TimeoutExpired:
        return _empty_remote_audit(
            target_dir=target_dir,
            branch=branch,
            bridge_name=bridge_name,
            large_file_threshold_bytes=large_file_threshold_bytes,
            error=f"Remote metadata audit timed out after {metadata_timeout}s",
        )
    except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as error:
        return _empty_remote_audit(
            target_dir=target_dir,
            branch=branch,
            bridge_name=bridge_name,
            large_file_threshold_bytes=large_file_threshold_bytes,
            error=str(error),
        )

    audit = _parse_audit_output(
        metadata_result.stdout or "",
        target_dir=target_dir,
        branch=branch,
        bridge_name=bridge_name,
        large_file_threshold_bytes=large_file_threshold_bytes,
    )
    if metadata_result.returncode != 0:
        audit["probe_success"] = False
        audit["scan_complete"] = False
        audit["error"] = (
            metadata_result.stderr.strip()
            or metadata_result.stdout.strip()
            or "Remote metadata audit failed"
        )
        audit["commit_sha"] = commit_sha
        return audit

    if not audit.get("target_exists") or not audit.get("is_git_repo"):
        audit["commit_sha"] = commit_sha
        return audit

    try:
        scan_result = run_command_fn(
            _build_remote_sync_scan_command(target_dir, large_file_threshold_bytes),
            bridge_name=bridge_name,
            config=config,
            timeout=scan_timeout,
            capture_output=True,
            check=False,
            pass_stdin=True,
        )
    except subprocess.TimeoutExpired:
        audit["probe_success"] = False
        audit["scan_complete"] = False
        audit["error"] = f"Remote dirty scan timed out after {scan_timeout}s"
        audit["commit_sha"] = commit_sha
        return audit
    except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as error:
        audit["probe_success"] = False
        audit["scan_complete"] = False
        audit["error"] = str(error)
        audit["commit_sha"] = commit_sha
        return audit

    if scan_result.returncode != 0:
        audit["probe_success"] = False
        audit["scan_complete"] = False
        audit["error"] = (
            scan_result.stderr.strip() or scan_result.stdout.strip() or "Remote dirty scan failed"
        )
        audit["commit_sha"] = commit_sha
        return audit

    scan_audit = _parse_audit_output(
        scan_result.stdout or "",
        target_dir=target_dir,
        branch=branch,
        bridge_name=bridge_name,
        large_file_threshold_bytes=large_file_threshold_bytes,
    )
    audit["dirty_files"] = scan_audit.get("dirty_files", [])
    audit["remote_dirty_hashes"] = scan_audit.get("remote_dirty_hashes", {})
    audit["untracked_large_files"] = scan_audit.get("untracked_large_files", [])
    audit["scan_complete"] = True
    # The scan probe ran, but the large-file sweep never descends into the fixed
    # exclusion patterns, so its answer is partial by construction.
    audit["large_file_scan_complete"] = not _LARGE_FILE_SCAN_EXCLUDED_PATTERNS
    audit["large_file_scan_skipped_patterns"] = list(_LARGE_FILE_SCAN_EXCLUDED_PATTERNS)
    audit["commit_sha"] = commit_sha
    return audit


def _parse_remote_diff_output(stdout: str, paths: list[str]) -> dict[str, str]:
    remote_diffs: dict[str, str] = {}
    diff_path: str | None = None
    diff_lines: list[str] = []
    for raw_line in stdout.splitlines():
        if raw_line.startswith(f"{_AUDIT_PREFIX}\t"):
            parts = raw_line.split("\t", 2)
            if len(parts) < 3:
                continue
            key = parts[1]
            value = parts[2]
            if key == "REMOTE_DIFF_BEGIN":
                try:
                    diff_path = paths[int(value)]
                except (IndexError, ValueError):
                    diff_path = None
                diff_lines = []
                continue
            if key == "REMOTE_DIFF_END":
                if diff_path is not None:
                    remote_diffs[diff_path] = "\n".join(diff_lines).strip()
                diff_path = None
                diff_lines = []
                continue
        elif diff_path is not None:
            diff_lines.append(raw_line)
    return remote_diffs


def _build_remote_sync_diff_command(
    target_dir: str,
    paths: list[str],
    max_lines: int,
) -> str:
    """Build a remote fact-emitter script for selected dirty diffs."""
    q_target_dir = shlex.quote(target_dir)
    q_prefix = shlex.quote(_AUDIT_PREFIX)
    q_max_lines = shlex.quote(str(max(1, max_lines)))
    quoted_paths = " ".join(shlex.quote(path) for path in paths)

    return f"""
set +e
prefix={q_prefix}
target_dir={q_target_dir}
max_lines={q_max_lines}
emit_diff() {{
  diff_index="$1"
  diff_path="$2"
  printf '%s\\t%s\\t%s\\n' "$prefix" REMOTE_DIFF_BEGIN "$diff_index"
  {{
    git diff --cached --no-ext-diff --no-color -- ":(literal)$diff_path" 2>/dev/null
    git diff --no-ext-diff --no-color -- ":(literal)$diff_path" 2>/dev/null
  }} | sed -n "1,${{max_lines}}p"
  printf '%s\\t%s\\t%s\\n' "$prefix" REMOTE_DIFF_END "$diff_index"
}}

if [ ! -d "$target_dir" ]; then
  exit 0
fi
cd "$target_dir" || exit 0
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 0
fi
set -- {quoted_paths}
diff_index=0
for diff_path in "$@"; do
  [ -n "$diff_path" ] || continue
  emit_diff "$diff_index" "$diff_path"
  diff_index=$((diff_index + 1))
done
""".strip()


def collect_remote_sync_diffs(
    target_dir: str,
    paths: list[str],
    *,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 60,
    max_lines: int = 80,
    run_command_fn: RemoteCommandRunner | None = None,
) -> dict[str, str]:
    """Collect bounded Bridge-side diffs for selected paths only."""
    if not paths:
        return {}
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command

    try:
        result = run_command_fn(
            _build_remote_sync_diff_command(target_dir, paths, max_lines),
            bridge_name=bridge_name,
            config=config,
            timeout=max(15, min(timeout, 60)),
            capture_output=True,
            check=False,
            pass_stdin=True,
        )
    except (
        subprocess.TimeoutExpired,
        subprocess.SubprocessError,
        OSError,
        TunnelError,
        ValueError,
    ):
        return {}
    if result.returncode != 0:
        return {}
    return _parse_remote_diff_output(result.stdout or "", paths)


def _extract_sha(stdout: str) -> Optional[str]:
    """Extract the last full SHA line from command output."""
    for line in reversed([ln.strip().lower() for ln in stdout.splitlines() if ln.strip()]):
        if _SHA_RE.match(line):
            return line
    return None


def _git_command_success(args: list[str], *, cwd: str | None = None) -> bool:
    """Run a local git command and return True when it succeeds."""
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as error:
        logger.debug("Local git command failed for %s: %s", args, error)
        return False


def _git_rev_count(revision_range: str, *, cwd: str | None = None) -> Optional[int]:
    """Count commits in a revision range, returning None on errors."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", revision_range],
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        if result.returncode != 0:
            return None
        return int((result.stdout or "").strip())
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        logger.debug("Unable to count revisions for %s: %s", revision_range, error)
        return None


def _create_git_bundle(bundle_file: str, revision: str, *, cwd: str | None = None) -> Optional[str]:
    """Create a git bundle and return an error message on failure."""
    try:
        subprocess.run(
            ["git", "bundle", "create", bundle_file, revision],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return None
    except subprocess.CalledProcessError as e:
        return e.stderr.strip() or e.stdout.strip() or str(e)


def _probe_remote_branch_tip(
    *,
    target_dir: str,
    branch: str,
    bridge_name: Optional[str],
    config: TunnelConfig,
    timeout: int,
    run_command_fn: RemoteCommandRunner | None = None,
) -> Optional[str]:
    """Best-effort probe for the current remote branch tip SHA."""
    q_target_dir = shlex.quote(target_dir)
    q_branch = shlex.quote(branch)
    run_command_fn = run_command_fn or run_ssh_command

    probe_cmd = f"""
set -e
cd {q_target_dir}
if [ ! -d .git ]; then
  exit 0
fi
branch={q_branch}
git rev-parse --verify "refs/heads/$branch" 2>/dev/null || true
"""

    try:
        probe = run_command_fn(
            probe_cmd.strip(),
            bridge_name=bridge_name,
            config=config,
            timeout=max(15, min(timeout, 30)),
            capture_output=True,
            check=False,
        )
    except (subprocess.SubprocessError, OSError, TunnelError) as error:
        logger.debug("Failed to probe remote branch tip for %s/%s: %s", target_dir, branch, error)
        return None

    if probe.returncode != 0:
        return None
    return _extract_sha(probe.stdout or "")


def sync_via_ssh(
    target_dir: str,
    branch: str,
    commit_sha: str,
    remote: str = "origin",
    force: bool = False,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 60,
    run_command_fn: RemoteCommandRunner | None = None,
) -> dict:
    """Sync code on Bridge via SSH ProxyCommand.

    Runs git fetch && git checkout on the remote Bridge machine.

    Args:
        target_dir: Target directory on Bridge (INSPIRE_TARGET_DIR)
        branch: Branch to sync
        commit_sha: Expected commit SHA after sync
        remote: Git remote to fetch from
        force: If True, use git reset --hard (discard local changes)
        bridge_name: Name of bridge to use (uses default if None)
        config: Tunnel configuration
        timeout: Command timeout in seconds

    Returns:
        Dict with keys:
        - success: bool
        - synced_sha: Optional[str]
        - error: Optional[str]

    Raises:
        TunnelNotAvailableError: If no bridge configured
        BridgeNotFoundError: If specified bridge not found
    """
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command

    q_target_dir = shlex.quote(target_dir)
    q_branch = shlex.quote(branch)
    q_remote = shlex.quote(remote)
    q_commit_sha = shlex.quote(commit_sha)

    update_cmd = (
        f"git reset --hard {q_commit_sha}" if force else f"git merge --ff-only {q_commit_sha}"
    )
    sync_cmd = f"""
set -e
cd {q_target_dir}
git fetch {q_remote} {q_branch}
git checkout {q_branch}
{update_cmd}
expected_sha={q_commit_sha}
actual_sha="$(git rev-parse HEAD)"
if [ "$actual_sha" != "$expected_sha" ]; then
  echo "Expected $expected_sha but got $actual_sha" >&2
  exit 1
fi
printf '%s\\n' "$actual_sha"
"""

    try:
        result = run_command_fn(
            sync_cmd.strip(),
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            capture_output=True,
            check=False,
        )

        if result.returncode == 0:
            # Extract the synced SHA from output (last line)
            lines = result.stdout.strip().split("\n")
            synced_sha = lines[-1].strip() if lines else ""

            return {
                "success": True,
                "synced_sha": synced_sha,
                "error": None,
            }
        else:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            return {
                "success": False,
                "synced_sha": None,
                "error": error_msg,
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "synced_sha": None,
            "error": f"Sync command timed out after {timeout}s",
        }
    except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as e:
        return {
            "success": False,
            "synced_sha": None,
            "error": str(e),
        }


def sync_via_ssh_bundle(
    target_dir: str,
    branch: str,
    commit_sha: str,
    force: bool = False,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 120,
    run_command_fn: RemoteCommandRunner | None = None,
    transfer_fn: FileTransferRunner | None = None,
    local_cwd: str | None = None,
) -> dict:
    """Sync code to Bridge via SSH tunnel using a local git bundle.

    This path works even when the bridge has no internet access.
    """
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command
    transfer_fn = transfer_fn or run_scp_transfer

    bundle_file = None
    bundle_mode = "full"
    remote_base_sha = _probe_remote_branch_tip(
        target_dir=target_dir,
        branch=branch,
        bridge_name=bridge_name,
        config=config,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )

    if remote_base_sha and remote_base_sha == commit_sha.lower():
        return {
            "success": True,
            "synced_sha": commit_sha.lower(),
            "error": None,
            "bundle_mode": "up_to_date",
        }

    bundle_rev = "HEAD"
    if remote_base_sha:
        has_remote_base = _git_command_success(
            ["git", "cat-file", "-e", f"{remote_base_sha}^{{commit}}"],
            cwd=local_cwd,
        )
        is_ancestor = _git_command_success(
            ["git", "merge-base", "--is-ancestor", remote_base_sha, commit_sha],
            cwd=local_cwd,
        )
        if has_remote_base and is_ancestor:
            incremental_range = f"{remote_base_sha}..{commit_sha}"
            incremental_count = _git_rev_count(incremental_range, cwd=local_cwd)
            if incremental_count == 0:
                # Defensive fast-path: avoid "cannot create empty bundle" errors.
                return {
                    "success": True,
                    "synced_sha": commit_sha.lower(),
                    "error": None,
                    "bundle_mode": "up_to_date",
                    "bundle_base_sha": remote_base_sha,
                }
            bundle_mode = "incremental"
            bundle_rev = incremental_range

    try:
        with tempfile.NamedTemporaryFile(
            prefix="inspire-sync-",
            suffix=".bundle",
            delete=False,
        ) as tmp:
            bundle_file = tmp.name

        error_msg = _create_git_bundle(bundle_file, bundle_rev, cwd=local_cwd)
        if error_msg and bundle_mode == "incremental":
            # Keep sync resilient: if incremental bundle creation fails for any reason
            # (network error, corrupt remote, etc.), do NOT assume "up_to_date" based
            # on commit count — always retry with a full bundle.
            full_error = _create_git_bundle(bundle_file, "HEAD", cwd=local_cwd)
            if full_error is None:
                bundle_mode = "full"
                bundle_rev = "HEAD"
                error_msg = None
            else:
                error_msg = full_error

        if error_msg:
            return {
                "success": False,
                "synced_sha": None,
                "error": f"Failed to create git bundle: {error_msg}",
            }

        remote_bundle = f"/tmp/{os.path.basename(bundle_file)}"
        scp_result = transfer_fn(
            local_path=bundle_file,
            remote_path=remote_bundle,
            download=False,
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
        )
        if scp_result.returncode != 0:
            return {
                "success": False,
                "synced_sha": None,
                "error": f"Failed to upload git bundle (scp exit {scp_result.returncode})",
            }

        q_target_dir = shlex.quote(target_dir)
        q_branch = shlex.quote(branch)
        q_commit_sha = shlex.quote(commit_sha)
        q_remote_bundle = shlex.quote(remote_bundle)

        update_cmd = (
            f"git reset --hard {q_commit_sha}" if force else f"git merge --ff-only {q_commit_sha}"
        )
        sync_cmd = f"""
set -e
trap 'rm -f {q_remote_bundle}' EXIT
cd {q_target_dir}
if [ ! -d .git ]; then
  echo "Target directory is not a git repository: {q_target_dir}" >&2
  exit 1
fi
git fetch {q_remote_bundle} {q_commit_sha}
git checkout {q_branch} || git checkout -b {q_branch}
{update_cmd}
expected_sha={q_commit_sha}
actual_sha="$(git rev-parse HEAD)"
if [ "$actual_sha" != "$expected_sha" ]; then
  echo "Expected $expected_sha but got $actual_sha" >&2
  exit 1
fi
printf '%s\\n' "$actual_sha"
"""

        result = run_command_fn(
            sync_cmd.strip(),
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            capture_output=True,
            check=False,
        )

        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            synced_sha = lines[-1].strip() if lines else ""
            return {
                "success": True,
                "synced_sha": synced_sha,
                "error": None,
                "bundle_mode": bundle_mode,
                "bundle_base_sha": remote_base_sha,
            }

        error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
        return {
            "success": False,
            "synced_sha": None,
            "error": error_msg,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "synced_sha": None,
            "error": f"Offline sync command timed out after {timeout}s",
        }
    except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as e:
        return {
            "success": False,
            "synced_sha": None,
            "error": str(e),
        }
    finally:
        if bundle_file:
            with contextlib.suppress(OSError):
                os.unlink(bundle_file)


def _resolve_git_dir_path(name: str, *, cwd: str | None) -> Optional[str]:
    """Resolve a path inside the git dir of the repository at *cwd*.

    ``git rev-parse --git-path`` answers relative to the working directory, so a
    subdirectory gets ``../.git/<name>`` and a linked worktree gets an absolute
    path under the main repository's ``.git/worktrees``. Both differ from a naive
    ``.git/<name>`` lookup, which is why the merge/rebase guard goes through here.
    Returns None when the path cannot be resolved (not a git repository, no git).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", name],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except (subprocess.SubprocessError, OSError) as error:
        logger.debug("Unable to resolve git path %s: %s", name, error)
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (Path(cwd) if cwd else Path.cwd()) / path
    return str(path)


def _in_progress_git_operation(cwd: str | None) -> Optional[str]:
    """Return "merge" or "rebase" when the repository at *cwd* has one in progress."""
    merge_head = _resolve_git_dir_path("MERGE_HEAD", cwd=cwd)
    if merge_head and os.path.exists(merge_head):
        return "merge"
    for state_dir in ("rebase-merge", "rebase-apply"):
        rebase_dir = _resolve_git_dir_path(state_dir, cwd=cwd)
        if rebase_dir and os.path.exists(rebase_dir):
            return "rebase"
    return None


def pull_from_bridge(
    target_dir: str,
    branch: str,
    local_sha: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 120,
    strategy: str = "merge",
    run_command_fn: RemoteCommandRunner | None = None,
    transfer_fn: FileTransferRunner | None = None,
    local_cwd: str | None = None,
) -> dict:
    """Pull changes from Bridge to the local repository at *local_cwd*.

    When someone has committed changes on the Bridge that are not in the local
    repository, this function creates a bundle on the Bridge, downloads it,
    and merges/rebases the changes into the local branch.

    Every local git command runs with ``cwd=local_cwd`` (the process working
    directory when None), so a pull started from an unrelated directory - or
    with ``--local`` pointing at another checkout - never touches that
    directory's repository.

    Args:
        target_dir: Target directory on Bridge (INSPIRE_TARGET_DIR)
        branch: Branch to pull
        local_sha: Current local HEAD SHA (to determine what Bridge has that we don't)
        bridge_name: Name of bridge to use (uses default if None)
        config: Tunnel configuration
        timeout: Command timeout in seconds
        strategy: Merge strategy - "merge" or "rebase"
        local_cwd: Local repository to pull into (process working directory when None)

    Returns:
        Dict with keys:
        - success: bool
        - pulled_sha: Optional[str] - SHA after pull
        - error: Optional[str]
    """
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command
    transfer_fn = transfer_fn or run_scp_transfer

    # Validate local_sha
    if not _SHA_RE.match(local_sha.lower()):
        return {
            "success": False,
            "pulled_sha": None,
            "error": f"Invalid local SHA: {local_sha}",
        }

    # Reject before any transfer when a merge or rebase is already in progress:
    # integrating Bridge changes on top would fail, and the failure cleanup must
    # never abort an operation the user already had open.
    in_progress = _in_progress_git_operation(local_cwd)
    if in_progress == "merge":
        return {
            "success": False,
            "pulled_sha": None,
            "error": "Merge in progress. Resolve or abort (git merge --abort) before pulling.",
        }
    if in_progress == "rebase":
        return {
            "success": False,
            "pulled_sha": None,
            "error": "Rebase in progress. Resolve or abort (git rebase --abort) before pulling.",
        }

    # Probe Bridge's current HEAD
    bridge_sha = _probe_remote_branch_tip(
        target_dir=target_dir,
        branch=branch,
        bridge_name=bridge_name,
        config=config,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )

    if not bridge_sha:
        return {
            "success": True,
            "pulled_sha": local_sha,
            "error": None,
            "message": "Bridge has no commits for this branch",
        }

    if bridge_sha == local_sha.lower():
        return {
            "success": True,
            "pulled_sha": local_sha,
            "error": None,
            "message": "Bridge is at same commit as local",
        }

    # Check if Bridge HEAD is an ancestor of local (i.e., local is ahead)
    try:
        is_ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", bridge_sha, local_sha],
            capture_output=True,
            check=False,
            cwd=local_cwd,
        )
        if is_ancestor.returncode == 0:
            return {
                "success": True,
                "pulled_sha": local_sha,
                "error": None,
                "message": "Local is ahead of Bridge, no pull needed",
            }
    except (subprocess.SubprocessError, OSError):
        pass

    # Bridge has commits that local doesn't have - need to pull
    q_target_dir = shlex.quote(target_dir)
    q_branch = shlex.quote(branch)
    q_local_sha = shlex.quote(local_sha)

    # Create a bundle on the Bridge from local_sha..bridge_sha
    # Use high-entropy filename to avoid symlink attacks
    remote_bundle = f"/tmp/inspire-pull-{os.urandom(16).hex()}.bundle"
    q_remote_bundle = shlex.quote(remote_bundle)

    # Use unique temp branch name to avoid collisions
    temp_branch = f"inspire-pull-{os.urandom(8).hex()}"

    # git bundle create refuses raw SHAs on the included side ("Refusing to
    # create an empty bundle"); it needs a named ref, and the probed branch is
    # exactly the ref bridge_sha was read from.
    create_bundle_cmd = f"""
set -e
cd {q_target_dir}
git bundle create {q_remote_bundle} {q_local_sha}..{q_branch}
"""

    local_bundle: str | None = None
    try:
        bundle_fd, local_bundle = tempfile.mkstemp(prefix="inspire-pull-", suffix=".bundle")
        os.close(bundle_fd)

        # Create bundle on Bridge
        result = run_command_fn(
            create_bundle_cmd.strip(),
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            # Clean up remote bundle
            run_command_fn(
                f"rm -f {q_remote_bundle}",
                bridge_name=bridge_name,
                config=config,
                timeout=30,
                capture_output=True,
                check=False,
            )
            return {
                "success": False,
                "pulled_sha": None,
                "error": f"Failed to create pull bundle on Bridge: {error_msg}",
            }

        # Download bundle from Bridge
        scp_result = transfer_fn(
            local_path=local_bundle,
            remote_path=remote_bundle,
            download=True,
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
        )

        # Always clean up remote bundle
        run_command_fn(
            f"rm -f {q_remote_bundle}",
            bridge_name=bridge_name,
            config=config,
            timeout=30,
            capture_output=True,
            check=False,
        )

        if scp_result.returncode != 0:
            return {
                "success": False,
                "pulled_sha": None,
                "error": f"Failed to download pull bundle from Bridge (scp exit {scp_result.returncode})",
            }

        # Fetch the bundle locally using SHA to handle detached HEAD
        fetch_result = subprocess.run(
            ["git", "fetch", local_bundle, f"{bridge_sha}:{temp_branch}"],
            capture_output=True,
            text=True,
            check=False,
            cwd=local_cwd,
        )

        if fetch_result.returncode != 0:
            return {
                "success": False,
                "pulled_sha": None,
                "error": f"Failed to fetch from pull bundle: {fetch_result.stderr}",
            }

        # Merge or rebase
        if strategy == "rebase":
            merge_result = subprocess.run(
                ["git", "rebase", temp_branch],
                capture_output=True,
                text=True,
                check=False,
                cwd=local_cwd,
            )
        else:
            merge_result = subprocess.run(
                ["git", "merge", "--no-edit", temp_branch],
                capture_output=True,
                text=True,
                check=False,
                cwd=local_cwd,
            )

        if merge_result.returncode != 0:
            # The operation above was started by this pull (the guard rejected
            # pre-existing merges/rebases), so aborting restores the pre-pull state.
            if strategy == "rebase":
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    capture_output=True,
                    check=False,
                    cwd=local_cwd,
                )
            else:
                subprocess.run(
                    ["git", "merge", "--abort"],
                    capture_output=True,
                    check=False,
                    cwd=local_cwd,
                )
            # Clean up temp branch
            subprocess.run(
                ["git", "branch", "-D", temp_branch],
                capture_output=True,
                check=False,
                cwd=local_cwd,
            )
            return {
                "success": False,
                "pulled_sha": None,
                "error": f"Failed to {strategy} Bridge changes: {merge_result.stderr}",
            }

        # Success - clean up temp branch
        subprocess.run(
            ["git", "branch", "-D", temp_branch],
            capture_output=True,
            check=False,
            cwd=local_cwd,
        )

        new_sha_result = subprocess.run(
            ["git", "rev-parse", branch],
            capture_output=True,
            text=True,
            check=False,
            cwd=local_cwd,
        )
        new_sha = new_sha_result.stdout.strip() if new_sha_result.returncode == 0 else local_sha
        return {
            "success": True,
            "pulled_sha": new_sha,
            "error": None,
            "message": f"Pulled Bridge changes via {strategy}",
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "pulled_sha": None,
            "error": f"Pull from Bridge timed out after {timeout}s",
        }
    except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as e:
        return {
            "success": False,
            "pulled_sha": None,
            "error": str(e),
        }
    finally:
        # Best-effort cleanup
        if local_bundle and os.path.exists(local_bundle):
            with contextlib.suppress(OSError):
                os.unlink(local_bundle)
        # Clean up temp branch if still exists
        subprocess.run(
            ["git", "branch", "-D", temp_branch],
            capture_output=True,
            check=False,
            cwd=local_cwd,
        )
        # Clean up remote bundle if still exists
        run_command_fn(
            f"rm -f {q_remote_bundle}",
            bridge_name=bridge_name,
            config=config,
            timeout=30,
            capture_output=True,
            check=False,
        )
