"""rtunnel binary helpers for SSH ProxyCommand access."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from inspire.config import Config

from .config import load_tunnel_config
from .models import TunnelConfig, TunnelError

# nightly release includes stdio:// mode for SSH ProxyCommand support
DEFAULT_RTUNNEL_DOWNLOAD_URL = (
    "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz"
)


def _get_rtunnel_download_url() -> str:
    """Get the rtunnel download URL from config or environment.

    Returns:
        Download URL for rtunnel binary
    """
    # Check environment variable first (highest priority)
    env_url = os.environ.get("INSPIRE_RTUNNEL_DOWNLOAD_URL")
    if env_url:
        return env_url

    # Try to load from config files
    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        if config.rtunnel_download_url:
            return config.rtunnel_download_url
    except Exception:
        pass

    # Use default
    return DEFAULT_RTUNNEL_DOWNLOAD_URL


def _ensure_rtunnel_binary(config: TunnelConfig) -> Path:
    """Ensure rtunnel binary exists, download if needed."""
    if config.rtunnel_bin.exists() and os.access(config.rtunnel_bin, os.X_OK):
        return config.rtunnel_bin

    # Download rtunnel
    config.rtunnel_bin.parent.mkdir(parents=True, exist_ok=True)

    try:
        import tarfile
        import tempfile
        import urllib.request

        # Download tar.gz and extract
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            urllib.request.urlretrieve(_get_rtunnel_download_url(), tmp.name)
            with tarfile.open(tmp.name, "r:gz") as tar:
                # Extract the rtunnel binary (should be the only file or named rtunnel*)
                for member in tar.getmembers():
                    if member.isfile() and "rtunnel" in member.name:
                        # Extract to a temp location first
                        extracted = tar.extractfile(member)
                        if extracted:
                            config.rtunnel_bin.write_bytes(extracted.read())
                            config.rtunnel_bin.chmod(0o755)
                            break
            # Clean up temp file
            Path(tmp.name).unlink(missing_ok=True)

        if not config.rtunnel_bin.exists():
            raise TunnelError("rtunnel binary not found in archive")

        return config.rtunnel_bin
    except Exception as e:
        raise TunnelError(f"Failed to download rtunnel: {e}")


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
