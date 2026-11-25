"""rtunnel binary helpers for SSH ProxyCommand access."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from inspire.config.rtunnel_defaults import expected_rtunnel_sha256
from inspire.config.ssh_runtime import (
    DEFAULT_RTUNNEL_DOWNLOAD_URL as CONFIG_DEFAULT_RTUNNEL_DOWNLOAD_URL,
    resolve_ssh_runtime_config,
)

from .config import load_tunnel_config
from .models import TunnelConfig, TunnelError

logger = logging.getLogger(__name__)

# The pinned release carries stdio:// mode, which is what lets OpenSSH use it as a
# ProxyCommand. See inspire/config/rtunnel_defaults.py for why the version is pinned.
DEFAULT_RTUNNEL_DOWNLOAD_URL = CONFIG_DEFAULT_RTUNNEL_DOWNLOAD_URL


def _get_rtunnel_download_url() -> str:
    """Get the rtunnel download URL from resolved SSH runtime config.

    Returns:
        Download URL for rtunnel binary
    """
    try:
        return resolve_ssh_runtime_config().rtunnel_download_url
    except Exception as e:
        logger.debug("Failed to resolve rtunnel download URL from config: %s", e)

    # Use default
    return DEFAULT_RTUNNEL_DOWNLOAD_URL


def _is_rtunnel_binary_usable(path: Path) -> bool:
    """Check if an rtunnel binary exists, is executable, and runs successfully.

    Uses ``rtunnel --help`` (exits 0) as the smoke test.
    """
    if not path.exists() or not os.access(path, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [str(path), "--help"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception as e:
        logger.debug("rtunnel binary test failed for %s: %s", path, e)
        return False


def _sha256_file(path: Path) -> str:
    """Streamed sha256 of a file, for verifying a downloaded artefact before exec'ing it."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_rtunnel_binary(archive: Path, destination: Path) -> None:
    """Copy just the rtunnel binary out of a release archive, nothing else."""
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile() and "rtunnel" in Path(member.name).name:
                handle = tar.extractfile(member)
                if handle is None:  # pragma: no cover - defensive for odd archives
                    continue
                destination.write_bytes(handle.read())
                destination.chmod(0o755)
                return
    raise TunnelError("rtunnel binary not found in archive")


def _ensure_rtunnel_binary(config: TunnelConfig) -> Path:
    """Ensure the rtunnel binary exists, downloading and verifying it if needed.

    Verification matters here more than in most download paths: this binary is exec'd to
    carry a live Jupyter token through the tunnel, so an unverified artefact would run with
    our credentials. Only sources we pin are checked; a mirror URL has no reviewed digest, so
    it is logged with its observed sha256 rather than blocked (and ``config check`` already
    reports that key when it is set outside the config files).
    """
    if _is_rtunnel_binary_usable(config.rtunnel_bin):
        return config.rtunnel_bin

    # Delete stale binary before re-download
    config.rtunnel_bin.unlink(missing_ok=True)
    config.rtunnel_bin.parent.mkdir(parents=True, exist_ok=True)

    url = _get_rtunnel_download_url()
    expected = expected_rtunnel_sha256(url)
    archive: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            archive = Path(tmp.name)
        urllib.request.urlretrieve(url, archive)

        actual = _sha256_file(archive)
        if expected is None:
            logger.info(
                "rtunnel source has no pinned checksum, running unverified: %s (sha256 %s)",
                url,
                actual,
            )
        elif actual != expected:
            raise TunnelError(
                "rtunnel checksum verification failed; refusing to exec an unverified binary\n"
                f"  url:      {url}\n"
                f"  expected: {expected}\n"
                f"  actual:   {actual}\n"
                "The pin lives in inspire/config/rtunnel_defaults.py and is reviewed together "
                "with .forgejo/ci-scripts/rtunnel_watch.sh. If upstream genuinely republished "
                "this release, confirm it with the rtunnel maintainers and update the pin in a "
                "reviewed commit. To install from a mirror instead, set [ssh] "
                "rtunnel_download_url - its sha256 is then reported so you can compare it."
            )
        else:
            logger.debug("rtunnel archive verified: sha256 %s", actual)

        _extract_rtunnel_binary(archive, config.rtunnel_bin)

        if expected is not None:
            # Provenance for support: which artefact produced the binary on this machine.
            sidecar = config.rtunnel_bin.with_suffix(".sha256")
            sidecar.write_text(f"{expected}  {url}\n", encoding="utf-8")
            sidecar.chmod(0o600)

        return config.rtunnel_bin
    except TunnelError:
        raise
    except Exception as e:
        raise TunnelError(f"Failed to download rtunnel: {e}") from e
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)


def get_rtunnel_path(config: Optional[TunnelConfig] = None) -> Path:
    """Get rtunnel binary path, downloading if needed.

    Args:
        config: Tunnel configuration

    Returns:
        Path to rtunnel binary

    Raises:
        TunnelError: If rtunnel cannot be found or downloaded
    """
    if config is None:
        config = load_tunnel_config()
    return _ensure_rtunnel_binary(config)
