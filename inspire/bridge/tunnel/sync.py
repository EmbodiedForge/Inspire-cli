"""Sync helpers implemented over SSH tunnel access."""

from __future__ import annotations

import subprocess
from typing import Optional

from .config import load_tunnel_config
from .models import TunnelConfig
from .ssh_exec import run_ssh_command


def sync_via_ssh(
    target_dir: str,
    branch: str,
    commit_sha: str,
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

    # Build the sync command
    if force:
        sync_cmd = f"""
cd "{target_dir}" && \
git fetch --all && \
git checkout "{branch}" && \
git reset --hard "origin/{branch}" && \
git rev-parse HEAD
"""
    else:
        sync_cmd = f"""
cd "{target_dir}" && \
git fetch --all && \
git checkout "{branch}" && \
git pull --ff-only && \
git rev-parse HEAD
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
