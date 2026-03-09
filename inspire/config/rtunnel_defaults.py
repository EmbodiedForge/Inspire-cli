"""Platform-aware rtunnel download URL defaults."""

from __future__ import annotations

import platform

RTUNNEL_RELEASE_BASE_URL = "https://github.com/Sarfflow/rtunnel/releases/download/nightly"


def _normalize_arch(machine: str) -> str:
    """Normalize a machine architecture string to rtunnel's naming convention.

    Args:
        machine: Raw architecture string (e.g. from platform.machine()).

    Returns:
        Normalized architecture: ``"amd64"`` or ``"arm64"``.

    Raises:
        ValueError: If the architecture is unrecognized.
    """
    lower = machine.lower()
    if lower in ("arm64", "aarch64"):
        return "arm64"
    if lower in ("x86_64", "amd64"):
        return "amd64"
    raise ValueError(f"Unsupported architecture: {machine}")


def _normalize_os(system: str) -> str:
    """Normalize an OS name to rtunnel's naming convention.

    Args:
        system: Raw OS name (e.g. from platform.system()).

    Returns:
        Normalized OS: ``"linux"`` or ``"darwin"``.

    Raises:
        ValueError: If the OS is unsupported.
    """
    lower = system.lower()
    if lower == "darwin":
        return "darwin"
    if lower == "linux":
        return "linux"
    raise ValueError(f"Unsupported OS: {system}")


def default_rtunnel_download_url() -> str:
    """Build the rtunnel download URL for the LOCAL machine.

    Returns:
        Full URL to the platform-appropriate rtunnel tar.gz archive.

    Raises:
        ValueError: If the current OS or architecture is unsupported.
    """
    os_name = _normalize_os(platform.system())
    arch = _normalize_arch(platform.machine())
    return f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-{os_name}-{arch}.tar.gz"


def rtunnel_download_url_shell_snippet() -> str:
    """Return a shell snippet that sets ``$RTUNNEL_DOWNLOAD_URL`` based on uname.

    This is intended for shell commands run INSIDE remote containers where
    the platform may differ from the local machine.
    """
    return (
        '_RTUNNEL_OS=$(uname -s | tr "[:upper:]" "[:lower:]"); '
        "_RTUNNEL_ARCH=$(uname -m); "
        'case "$_RTUNNEL_ARCH" in x86_64|amd64) _RTUNNEL_ARCH=amd64;; '
        "arm64|aarch64) _RTUNNEL_ARCH=arm64;; esac; "
        f'RTUNNEL_DOWNLOAD_URL="{RTUNNEL_RELEASE_BASE_URL}'
        '/rtunnel-${_RTUNNEL_OS}-${_RTUNNEL_ARCH}.tar.gz"'
    )


__all__ = [
    "RTUNNEL_RELEASE_BASE_URL",
    "default_rtunnel_download_url",
    "rtunnel_download_url_shell_snippet",
]
