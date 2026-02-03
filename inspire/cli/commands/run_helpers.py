"""Helpers for `inspire run` (subprocess + git utilities)."""

from __future__ import annotations

import os
import shutil
import subprocess


def _get_current_branch() -> str | None:
    """Get the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


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
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _get_inspire_executable() -> str | None:
    """Find the inspire CLI executable in PATH."""
    return shutil.which("inspire")


def _run_inspire_subcommand(args: list[str]) -> int:
    """Run an inspire subcommand as a subprocess."""
    exe = _get_inspire_executable()
    if not exe:
        raise RuntimeError("Cannot find 'inspire' executable in PATH")

    proc = subprocess.run([exe, *args])
    return proc.returncode


def _exec_inspire_subcommand(args: list[str]) -> None:
    """Exec (replace process) with an inspire subcommand."""
    exe = _get_inspire_executable()
    if not exe:
        raise RuntimeError("Cannot find 'inspire' executable in PATH")

    os.execv(exe, [exe, *args])
