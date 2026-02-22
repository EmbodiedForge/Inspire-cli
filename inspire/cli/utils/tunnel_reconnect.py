"""Reconnect/rebuild helpers for SSH tunnels backed by notebook rtunnel."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig, save_tunnel_config
from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession

DEFAULT_RTUNNEL_PORT = 31337
SSH_DISCONNECT_RETURN_CODES = {255}
_PROXY_PORT_RE = re.compile(r"/proxy/(\d+)/")


def extract_rtunnel_port(proxy_url: str, *, default_port: int = DEFAULT_RTUNNEL_PORT) -> int:
    """Extract `/proxy/<port>/` from a proxy URL; fall back to *default_port*."""
    match = _PROXY_PORT_RE.search(str(proxy_url))
    if match:
        try:
            port = int(match.group(1))
            if 0 < port <= 65535:
                return port
        except ValueError:
            pass
    return default_port


def should_attempt_ssh_reconnect(
    returncode: int,
    *,
    interactive: bool,
    allow_non_interactive: bool = False,
) -> bool:
    """Return True if this SSH exit code indicates connection loss."""
    if returncode not in SSH_DISCONNECT_RETURN_CODES:
        return False
    return interactive or allow_non_interactive


def retry_pause_seconds(attempt: int, *, base_pause: float, progressive: bool = True) -> float:
    """Compute pause before retrying reconnect/rebuild."""
    base = max(0.0, float(base_pause))
    if not progressive:
        return base
    return base + float(max(0, attempt - 1))


def load_ssh_public_key_material(pubkey_path: Optional[str] = None) -> str:
    """Load SSH public key content from an explicit or default local path."""
    if pubkey_path:
        candidates = [Path(pubkey_path).expanduser()]
    else:
        candidates = [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]

    for path in candidates:
        if not path.exists():
            continue
        key = path.read_text(encoding="utf-8", errors="ignore").strip()
        if key:
            return key

    raise ValueError(
        "No SSH public key found. Provide --pubkey PATH or generate one with 'ssh-keygen'."
    )


def rebuild_notebook_bridge_profile(
    *,
    bridge_name: str,
    bridge: BridgeProfile,
    tunnel_config: TunnelConfig,
    session: WebSession,
    ssh_public_key: str,
    ssh_runtime: SshRuntimeConfig,
    timeout: int = 300,
    headless: bool = True,
) -> BridgeProfile:
    """Rebuild a notebook-backed bridge profile and persist it to tunnel config."""
    notebook_id = str(getattr(bridge, "notebook_id", "") or "").strip()
    if not notebook_id:
        raise ValueError(f"Bridge '{bridge_name}' is not notebook-backed (missing notebook_id).")

    tunnel_port = bridge.rtunnel_port or extract_rtunnel_port(bridge.proxy_url)
    proxy_url = browser_api_module.setup_notebook_rtunnel(
        notebook_id=notebook_id,
        port=tunnel_port,
        ssh_port=bridge.ssh_port,
        ssh_public_key=ssh_public_key,
        ssh_runtime=ssh_runtime,
        session=session,
        headless=headless,
        timeout=timeout,
    )

    updated = BridgeProfile(
        name=bridge_name,
        proxy_url=proxy_url,
        ssh_user=bridge.ssh_user,
        ssh_port=bridge.ssh_port,
        has_internet=bridge.has_internet,
        notebook_id=notebook_id,
        rtunnel_port=tunnel_port,
    )
    tunnel_config.add_bridge(updated)
    save_tunnel_config(tunnel_config)
    return updated


__all__ = [
    "DEFAULT_RTUNNEL_PORT",
    "extract_rtunnel_port",
    "load_ssh_public_key_material",
    "rebuild_notebook_bridge_profile",
    "retry_pause_seconds",
    "should_attempt_ssh_reconnect",
]
