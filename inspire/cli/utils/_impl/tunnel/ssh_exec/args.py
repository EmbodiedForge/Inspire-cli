"""Build SSH command arguments (ProxyCommand mode)."""

from __future__ import annotations

from typing import Optional

from inspire.cli.utils.tunnel_models import TunnelConfig
from .core import _build_ssh_base_args, _resolve_bridge_and_proxy


def get_ssh_command_args(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    remote_command: Optional[str] = None,
) -> list[str]:
    """Build SSH command arguments with ProxyCommand."""
    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config)
    args = _build_ssh_base_args(bridge=bridge, proxy_cmd=proxy_cmd, batch_mode=False)
    if remote_command:
        args.append(remote_command)
    return args


__all__ = ["get_ssh_command_args"]
