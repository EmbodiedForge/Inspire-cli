"""Tunnel domain models and errors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, SupportsIndex, SupportsInt, cast


class TunnelError(Exception):
    """Base exception for tunnel-related errors."""


class TunnelNotAvailableError(TunnelError):
    """Raised when tunnel is not available or not running."""


class BridgeNotFoundError(TunnelError):
    """Raised when specified bridge profile is not found."""


# Default configuration
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22222
DEFAULT_RTUNNEL_PORT = 31337
_PROXY_PORT_RE = re.compile(r"/proxy/(\d+)/")


def _coerce_rtunnel_port(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        port = int(cast(str | bytes | bytearray | SupportsInt | SupportsIndex, value))
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    return port


def _proxy_port_from_url(proxy_url: str) -> Optional[int]:
    match = _PROXY_PORT_RE.search(str(proxy_url))
    if not match:
        return None
    return _coerce_rtunnel_port(match.group(1))


def has_internet_for_gpu_type(gpu_type: str) -> bool:
    """Determine if a GPU type has internet access.

    On Inspire platform:
    - CPU, 4090: has internet
    - H100, H200: no internet

    Args:
        gpu_type: GPU type string (e.g., "H200", "H100-SXM", "4090", "")

    Returns:
        True if the GPU type has internet access, False otherwise.
    """
    if not gpu_type:
        return True  # Default to True for CPU/unknown

    gpu_upper = gpu_type.upper()

    # H100/H200 don't have internet
    if "H100" in gpu_upper or "H200" in gpu_upper:
        return False

    # CPU and 4090 have internet
    return True


@dataclass
class BridgeProfile:
    """A single bridge configuration."""

    name: str
    proxy_url: str
    ssh_user: str = DEFAULT_SSH_USER
    ssh_port: int = DEFAULT_SSH_PORT
    identity_file: Optional[str] = None
    # Optional notebook binding for saved notebook SSH profiles.
    notebook_id: Optional[str] = None
    # Cached notebook name, populated at bridge creation time from the API.
    # Avoids a web API round-trip per bridge on every `bridge list` call.
    notebook_name: str = ""
    # Optional rtunnel server port in the notebook.
    rtunnel_port: Optional[int] = None
    # GPU type for internet access inference (e.g. "H100", "H200", "4090").
    # When set, has_internet is computed from has_internet_for_gpu_type().
    gpu_type: str = ""
    # Legacy override for has_internet, used only when gpu_type is empty (a bridge
    # added by URL, where the platform never told us the GPU). `tunnel add
    # --no-internet` writes it. There is deliberately no way to set it on a bridge
    # that has gpu_type: the getter would ignore it.
    _has_internet_stored: Optional[bool] = None
    # Scheduling priority from Inspire API (1-3=low/unstable, 7-10=high/stable).
    # Set from notebook project when bridge is created from a notebook.
    # None means priority has not been populated yet.
    priority: Optional[int] = None

    @property
    def has_internet(self) -> bool:
        """Whether this bridge has internet access.

        Derived from gpu_type when available (computed).
        Falls back to the legacy stored value for backward compat.
        """
        if self.gpu_type:
            return has_internet_for_gpu_type(self.gpu_type)
        if self._has_internet_stored is not None:
            return self._has_internet_stored
        return True  # conservative default

    @has_internet.setter
    def has_internet(self, value: bool) -> None:
        # When gpu_type is set the getter derives the answer from it and never
        # consults this field, so accepting the write would turn a wrong value
        # into a silent no-op. Fail loudly instead.
        if self.gpu_type:
            raise ValueError(
                f"Bridge {self.name!r} derives internet access from gpu_type="
                f"{self.gpu_type!r}; setting has_internet would be ignored. "
                "Set gpu_type instead."
            )
        self._has_internet_stored = value

    def to_dict(self) -> dict:
        payload = {
            "name": self.name,
            "proxy_url": self.proxy_url,
            "ssh_user": self.ssh_user,
            "ssh_port": self.ssh_port,
            "has_internet": self.has_internet,
        }
        if self.identity_file:
            payload["identity_file"] = self.identity_file
        if self.notebook_id:
            payload["notebook_id"] = self.notebook_id
        if self.notebook_name:
            payload["notebook_name"] = self.notebook_name
        if self.rtunnel_port is not None:
            payload["rtunnel_port"] = self.rtunnel_port
        if self.gpu_type:
            payload["gpu_type"] = self.gpu_type
        if self.priority is not None:
            payload["priority"] = self.priority
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "BridgeProfile":
        proxy_url = data["proxy_url"]
        rtunnel_port = (
            _coerce_rtunnel_port(data.get("rtunnel_port"))
            or _proxy_port_from_url(proxy_url)
            or DEFAULT_RTUNNEL_PORT
        )
        return cls(
            name=data["name"],
            proxy_url=proxy_url,
            ssh_user=data.get("ssh_user", DEFAULT_SSH_USER),
            ssh_port=data.get("ssh_port", DEFAULT_SSH_PORT),
            identity_file=data.get("identity_file"),
            notebook_id=data.get("notebook_id"),
            notebook_name=data.get("notebook_name", ""),
            rtunnel_port=rtunnel_port,
            gpu_type=data.get("gpu_type", ""),
            _has_internet_stored=data.get("has_internet"),
            priority=data.get("priority"),
        )


@dataclass
class TunnelConfig:
    """Tunnel configuration with multiple bridge profiles."""

    bridges: dict[str, BridgeProfile] = field(default_factory=dict)
    default_bridge: Optional[str] = None
    account: Optional[str] = None

    # Paths
    config_dir: Path = field(default_factory=lambda: Path.home() / ".inspire")

    @property
    def config_file(self) -> Path:
        if self.account:
            return self.config_dir / f"bridges-{self.account}.json"
        return self.config_dir / "bridges.json"

    @property
    def rtunnel_bin(self) -> Path:
        return Path.home() / ".local" / "bin" / "rtunnel"

    def get_bridge(self, name: Optional[str] = None) -> Optional[BridgeProfile]:
        """Get a bridge profile by name, or the default if name is None."""
        if name:
            return self.bridges.get(name)
        elif self.default_bridge:
            return self.bridges.get(self.default_bridge)
        elif len(self.bridges) == 1:
            # If only one bridge, use it as default
            return next(iter(self.bridges.values()))
        return None

    def add_bridge(self, profile: BridgeProfile) -> None:
        """Add or update a bridge profile."""
        self.bridges[profile.name] = profile
        # Set as default if it's the first bridge
        if self.default_bridge is None:
            self.default_bridge = profile.name

    def remove_bridge(self, name: str) -> bool:
        """Remove a bridge profile. Returns True if removed."""
        if name in self.bridges:
            del self.bridges[name]
            if self.default_bridge == name:
                # Set new default
                self.default_bridge = next(iter(self.bridges.keys()), None)
            return True
        return False

    def list_bridges(self) -> list[BridgeProfile]:
        """List all bridge profiles."""
        return list(self.bridges.values())

    def get_bridge_with_internet(self) -> Optional[BridgeProfile]:
        """Get a bridge with internet access.

        Prefers the default bridge if it has internet access.
        Otherwise returns the first bridge with internet access.

        Returns:
            BridgeProfile with internet, or None if no such bridge exists
        """
        # Prefer default bridge if it has internet
        if self.default_bridge:
            default = self.bridges.get(self.default_bridge)
            if default and default.has_internet:
                return default
        # Otherwise, find any bridge with internet
        for bridge in self.bridges.values():
            if bridge.has_internet:
                return bridge
        return None
