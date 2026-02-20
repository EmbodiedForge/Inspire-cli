"""Sync helpers implemented over SSH tunnel access."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from typing import Optional

from .config import load_tunnel_config
from .models import TunnelConfig
from .scp import run_scp_transfer
from .ssh_exec import run_ssh_command


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
    except Exception as e:
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
    try:
        with tempfile.NamedTemporaryFile(
            prefix="inspire-sync-",
            suffix=".bundle",
            delete=False,
        ) as tmp:
            bundle_file = tmp.name

        try:
            subprocess.run(
                ["git", "bundle", "create", bundle_file, "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.strip() or e.stdout.strip() or str(e)
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
    except Exception as e:
        return {
            "success": False,
            "synced_sha": None,
            "error": str(e),
        }
    finally:
        if bundle_file:
            try:
                os.unlink(bundle_file)
            except OSError:
                pass
