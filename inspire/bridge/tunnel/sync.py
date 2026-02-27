"""Sync helpers implemented over SSH tunnel access."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import subprocess
import tempfile
from typing import Optional

from .config import load_tunnel_config
from .models import TunnelConfig, TunnelError
from .scp import run_scp_transfer
from .ssh_exec import run_ssh_command

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
logger = logging.getLogger(__name__)


def _extract_sha(stdout: str) -> Optional[str]:
    """Extract the last full SHA line from command output."""
    for line in reversed([ln.strip().lower() for ln in stdout.splitlines() if ln.strip()]):
        if _SHA_RE.match(line):
            return line
    return None


def _git_command_success(args: list[str]) -> bool:
    """Run a local git command and return True when it succeeds."""
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as error:
        logger.debug("Local git command failed for %s: %s", args, error)
        return False


def _git_rev_count(revision_range: str) -> Optional[int]:
    """Count commits in a revision range, returning None on errors."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", revision_range],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return int((result.stdout or "").strip())
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        logger.debug("Unable to count revisions for %s: %s", revision_range, error)
        return None


def _create_git_bundle(bundle_file: str, revision: str) -> Optional[str]:
    """Create a git bundle and return an error message on failure."""
    try:
        subprocess.run(
            ["git", "bundle", "create", bundle_file, revision],
            check=True,
            capture_output=True,
            text=True,
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
) -> Optional[str]:
    """Best-effort probe for the current remote branch tip SHA."""
    q_target_dir = shlex.quote(target_dir)
    q_branch = shlex.quote(branch)

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
        probe = run_ssh_command(
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
        result = run_ssh_command(
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
) -> dict:
    """Sync code to Bridge via SSH tunnel using a local git bundle.

    This path works even when the bridge has no internet access.
    """
    if config is None:
        config = load_tunnel_config()

    bundle_file = None
    bundle_mode = "full"
    remote_base_sha = _probe_remote_branch_tip(
        target_dir=target_dir,
        branch=branch,
        bridge_name=bridge_name,
        config=config,
        timeout=timeout,
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
            ["git", "cat-file", "-e", f"{remote_base_sha}^{{commit}}"]
        )
        is_ancestor = _git_command_success(
            ["git", "merge-base", "--is-ancestor", remote_base_sha, commit_sha]
        )
        if has_remote_base and is_ancestor:
            incremental_range = f"{remote_base_sha}..{commit_sha}"
            incremental_count = _git_rev_count(incremental_range)
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

        error_msg = _create_git_bundle(bundle_file, bundle_rev)
        if error_msg and bundle_mode == "incremental":
            incremental_count = _git_rev_count(bundle_rev)
            if incremental_count == 0:
                return {
                    "success": True,
                    "synced_sha": commit_sha.lower(),
                    "error": None,
                    "bundle_mode": "up_to_date",
                    "bundle_base_sha": remote_base_sha,
                }

            # Keep sync resilient: if incremental bundle creation fails,
            # retry with a full bundle before failing.
            full_error = _create_git_bundle(bundle_file, "HEAD")
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
        scp_result = run_scp_transfer(
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

        result = run_ssh_command(
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
