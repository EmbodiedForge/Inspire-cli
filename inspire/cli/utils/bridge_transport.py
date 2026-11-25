"""Shared bridge transport selection for agent-facing commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from inspire.bridge.tunnel import BridgeNotFoundError, BridgeProfile, TunnelConfig

BRIDGE_TRANSPORTS = ("auto", "ssh", "jupyter")


@dataclass(frozen=True)
class BridgeTransportSelection:
    requested: str
    selected: str
    bridge: BridgeProfile
    ssh_available: bool | None


def select_bridge_transport(
    *,
    requested: str,
    bridge_name: str | None,
    tunnel_config: TunnelConfig,
    ssh_probe: Callable[..., bool],
    probe_timeout: float = 2.0,
) -> BridgeTransportSelection:
    """Select SSH when healthy, otherwise use a notebook's Jupyter channel."""
    requested = requested.lower().strip()
    if requested not in BRIDGE_TRANSPORTS:
        raise ValueError(f"Unsupported bridge transport: {requested}")

    profile = tunnel_config.get_bridge(bridge_name)
    if profile is None:
        if bridge_name:
            raise BridgeNotFoundError(f"Bridge '{bridge_name}' not found.")
        raise BridgeNotFoundError("No bridge configured.")

    if requested == "ssh":
        return BridgeTransportSelection(
            requested=requested,
            selected="ssh",
            bridge=profile,
            ssh_available=None,
        )
    if requested == "jupyter":
        if not str(profile.notebook_id or "").strip():
            raise ValueError(
                f"Bridge '{profile.name}' has no notebook_id binding required by Jupyter transport."
            )
        return BridgeTransportSelection(
            requested=requested,
            selected="jupyter",
            bridge=profile,
            ssh_available=None,
        )

    # A notebook-bound profile with no proxy URL records an SSH-ingress-denied
    # notebook. It is intentionally usable through Jupyter without probing a
    # transport that cannot exist.
    if str(profile.notebook_id or "").strip() and not str(profile.proxy_url or "").strip():
        return BridgeTransportSelection(
            requested=requested,
            selected="jupyter",
            bridge=profile,
            ssh_available=False,
        )

    # Preserve legacy SSH/rebuild semantics for CPU, internet-capable, and
    # unknown old profiles.  The automatic Jupyter fallback is intentionally
    # narrow: it targets H100/H200-style no-internet notebook profiles where
    # the platform currently blocks the custom SSH proxy ingress.
    if profile.has_internet or not str(profile.notebook_id or "").strip():
        return BridgeTransportSelection(
            requested=requested,
            selected="ssh",
            bridge=profile,
            ssh_available=None,
        )

    try:
        ssh_available = ssh_probe(
            bridge_name=profile.name,
            config=tunnel_config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
            probe_timeout=probe_timeout,
        )
    except Exception:  # noqa: BLE001 - transport fallback must survive probe failures
        ssh_available = False

    selected = "ssh" if ssh_available else "jupyter"
    return BridgeTransportSelection(
        requested=requested,
        selected=selected,
        bridge=profile,
        ssh_available=ssh_available,
    )


__all__ = ["BRIDGE_TRANSPORTS", "BridgeTransportSelection", "select_bridge_transport"]
