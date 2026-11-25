"""Permissions for files that hold credentials.

Why this module exists
----------------------
``~/.inspire/bridges*.json`` stores ``proxy_url``, whose query string carries a live
Jupyter token, and that token is root inside the notebook. ``~/.config/inspire/config.toml``
can hold the platform password in plaintext. Both are secrets regardless of how plainly the
file is named.

Two independent ways these leaked, both measured on a real machine:

1. Older releases wrote bridge state from the process umask, so under the common desktop
   ``umask(002)`` the files landed 0664 - readable by every local user - one of them holding
   141 tokens. Saving writes 0600 now, but a file nobody re-saves stays loose forever.
2. Atomic writes *preserve* an existing file's mode (so a deliberate 0640 survives), which
   means one loose file - created by an editor, a ``cp``, or an old release - keeps
   re-issuing its own loose mode on every write, and the ``default_mode=0o600`` fallback
   never applies. Backups made outside this code path stay loose until something repairs
   them, which is what ``harden_private_dirs`` is for.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Name fragments that mark a file as ours-to-protect. Matching on fragments rather than an
# exact file list is deliberate: it also catches interrupted-save temporaries
# (``.bridges.json.tmp9``), quarantined copies (``jobs.json.corrupt-20260812-212105``) and
# timestamped config backups (``config.toml.bak-20260926T131027Z``), and it protects the next
# state file without anyone remembering to register it.
STATE_NAME_MARKERS = (".json", ".log", ".bridges", ".toml", ".sh")


def _is_protected_name(name: str) -> bool:
    return any(marker in name for marker in STATE_NAME_MARKERS)


def harden_state_permissions(root: Path) -> list[Path]:
    """Remove group/other access from our own state below *root*; return what changed.

    Repairs on *read* rather than waiting for a write that may never come. Covers the
    directory itself and its subdirectories (``logs/`` inherits the loose mode), and skips
    symlinks so a link pointing outside our state directory cannot have us chmod files that
    are not ours.

    Best-effort by design: a read-only filesystem, or a directory owned by someone else,
    must not turn every CLI command into a failure. Failures are logged at debug level and
    skipped, so callers never need to guard this call.
    """
    changed: list[Path] = []

    def tighten(path: Path, mode: int) -> bool:
        try:
            current = path.stat().st_mode
        except OSError as exc:
            logger.debug("Cannot stat %s: %s", path, exc)
            return False
        if not current & 0o077:
            return False
        try:
            os.chmod(path, mode)
        except OSError as exc:
            logger.debug("Could not tighten %s: %s", path, exc)
            return False
        return True

    if tighten(root, 0o700):
        changed.append(root)

    for path in sorted(root.rglob("*")):
        try:
            if path.is_symlink():
                continue
            if path.is_dir():
                tighten(path, 0o700)
            elif _is_protected_name(path.name) and tighten(path, 0o600):
                changed.append(path)
        except OSError as exc:
            logger.debug("Skipping %s while tightening state: %s", path, exc)

    if changed:
        logger.debug(
            "Tightened permissions on %d inspire state path(s): %s",
            len(changed),
            ", ".join(path.name for path in changed),
        )
    return changed


def private_state_dirs() -> list[Path]:
    """Directories that hold inspire credentials, whether or not they exist yet.

    The cache directory holds web-session cookies, rtunnel proxy URLs (whose query string
    carries a Jupyter token), runtime session state, and ``--debug`` logs, so it needs
    repair just as much as the config and bridge directories do.
    """
    dirs: list[Path] = []

    try:
        from inspire.config.models import Config

        dirs.append(Config.resolve_global_config_path().parent)
    except Exception as exc:  # pragma: no cover - defensive; a broken config must not hide the rest
        logger.debug("Could not resolve the global config directory: %s", exc)

    try:
        from inspire.bridge.tunnel.models import TunnelConfig

        dirs.append(TunnelConfig().config_dir)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not resolve the tunnel state directory: %s", exc)

    try:
        from inspire.platform.web.session.models import SESSION_CACHE_DIR

        dirs.append(SESSION_CACHE_DIR)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not resolve the session cache directory: %s", exc)

    try:
        from inspire.config.account_state import resolve_account_state_root

        dirs.append(resolve_account_state_root())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not resolve the account state directory: %s", exc)

    return dirs


def harden_private_dirs() -> list[Path]:
    """Repair permissions across every credential directory. Returns the changed paths."""
    changed: list[Path] = []
    for directory in private_state_dirs():
        if directory.exists():
            changed.extend(harden_state_permissions(directory))
    return changed
