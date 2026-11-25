"""Platform-aware rtunnel download URL defaults."""

from __future__ import annotations

import platform

# Pinned upstream release. This used to point at the release literally tagged ``nightly``,
# which turned out to be the worst of both worlds: the name floats, so nothing is
# reproducible, while the artefacts had not been rebuilt since 2025-12-05 - months older
# than the v1.1.0 tag we now use. A tag is immutable, so a pinned checksum can mean
# something, and upgrading is a deliberate one-line change reviewed like any other change.
RTUNNEL_RELEASE_VERSION = "v1.1.0"
RTUNNEL_RELEASE_BASE_URL = (
    f"https://github.com/Sarfflow/rtunnel/releases/download/{RTUNNEL_RELEASE_VERSION}"
)

# sha256 of each release archive, taken from the artefacts published with the tag above and
# cross-checked against .forgejo/ci-scripts/rtunnel-baseline.txt (a test asserts the two agree,
# so the reviewed baseline and the runtime pin cannot drift). The CLI execs this binary to
# carry credentials through the tunnel, so downloading it without verification would let a
# broken or substituted artefact run with our Jupyter token.
RTUNNEL_ARTIFACT_SHA256: dict[str, str] = {
    "linux-amd64": "5c5f5bd6e61f4a5880ae875296bd45294e6f5a2e502946c0d740bfdd906c953d",
    "linux-arm64": "e5547b807e40f889cd38541e93c3108438d0f37f8b488ff0b14579924743ec5c",
    "darwin-amd64": "21eb6218612ef887811a62cb7a2f64dc0886558da80e91087f0cc44f0ff08248",
    "darwin-arm64": "3403264ec5ad9387aba9fb3bdbe77d46c6f286cab324077914d963170978be14",
}


def rtunnel_artifact_name(os_name: str, arch: str) -> str:
    """Release archive name for a normalized ``(_normalize_os, _normalize_arch)`` pair."""
    return f"rtunnel-{os_name}-{arch}.tar.gz"


def expected_rtunnel_sha256(url: str) -> str | None:
    """Return the pinned checksum for *url*, or ``None`` when it is not one we pin.

    ``None`` means "we have no reviewed digest for this source", which callers treat as
    skip-with-note rather than reject: ``rtunnel_download_url`` exists so people on mirrors
    and on machines we do not publish for can still connect. It is not a silent opt-out -
    ``config check`` reports that key whenever it comes from the environment.
    """
    if not url.startswith(RTUNNEL_RELEASE_BASE_URL + "/"):
        return None
    name = url.rsplit("/", 1)[-1]
    for key, digest in RTUNNEL_ARTIFACT_SHA256.items():
        if name == rtunnel_artifact_name(*key.split("-", 1)):
            return digest
    return None


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
    return f"{RTUNNEL_RELEASE_BASE_URL}/{rtunnel_artifact_name(os_name, arch)}"


def _fallback_rtunnel_download_url() -> str:
    """Return an import-safe fallback URL for unsupported local platforms.

    We do not support every host OS, but config imports must still succeed so
    remote notebook/bootstrap flows can run. Preserve a recognized local
    architecture and fall back to the Linux archive naming.
    """
    try:
        arch = _normalize_arch(platform.machine())
    except ValueError:
        arch = "amd64"
    return f"{RTUNNEL_RELEASE_BASE_URL}/{rtunnel_artifact_name('linux', arch)}"


#: Cached result of ``default_rtunnel_download_url()`` for the current platform.
#: Falls back to a Linux archive with the detected architecture so module
#: import never crashes on unsupported local platforms.
try:
    DEFAULT_RTUNNEL_DOWNLOAD_URL = default_rtunnel_download_url()
except ValueError:
    DEFAULT_RTUNNEL_DOWNLOAD_URL = _fallback_rtunnel_download_url()


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
    "DEFAULT_RTUNNEL_DOWNLOAD_URL",
    "RTUNNEL_ARTIFACT_SHA256",
    "RTUNNEL_RELEASE_BASE_URL",
    "RTUNNEL_RELEASE_VERSION",
    "default_rtunnel_download_url",
    "expected_rtunnel_sha256",
    "rtunnel_artifact_name",
    "rtunnel_download_url_shell_snippet",
]
