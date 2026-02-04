"""Core helpers shared by SSH execution functions."""

from __future__ import annotations

import shlex
from typing import Optional

from inspire.cli.utils.tunnel_config import load_tunnel_config
from inspire.cli.utils.tunnel_models import (
    BridgeNotFoundError,
    BridgeProfile,
    TunnelConfig,
    TunnelNotAvailableError,
)
from inspire.cli.utils.tunnel_rtunnel import _ensure_rtunnel_binary
from inspire.cli.utils._impl.tunnel.ssh.proxy import _get_proxy_command


def _resolve_bridge_and_proxy(
    bridge_name: Optional[str],
    config: Optional[TunnelConfig],
) -> tuple[TunnelConfig, BridgeProfile, str]:
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        if bridge_name:
            raise BridgeNotFoundError(f"Bridge '{bridge_name}' not found")
        raise TunnelNotAvailableError(
            "No bridge configured. Run 'inspire tunnel add <name> <url>' first."
        )

    _ensure_rtunnel_binary(config)
    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=True)
    return config, bridge, proxy_cmd


def _wrap_command_in_login_shell(command: str) -> str:
    return f"LC_ALL=C LANG=C bash -l -c {shlex.quote(command)}"


def _build_ssh_base_args(
    *,
    bridge: BridgeProfile,
    proxy_cmd: str,
    batch_mode: bool = True,
) -> list[str]:
    args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ProxyCommand={proxy_cmd}",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(bridge.ssh_port),
        f"{bridge.ssh_user}@localhost",
    ]
    if batch_mode:
        args[5:5] = ["-o", "BatchMode=yes"]
    return args


__all__ = ["_build_ssh_base_args", "_resolve_bridge_and_proxy", "_wrap_command_in_login_shell"]
