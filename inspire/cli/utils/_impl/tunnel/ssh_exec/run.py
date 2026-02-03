"""Run commands via SSH ProxyCommand."""

from __future__ import annotations

import subprocess
from typing import Optional

from inspire.cli.utils.tunnel_models import TunnelConfig
from .core import (
    _build_ssh_base_args,
    _resolve_bridge_and_proxy,
    _wrap_command_in_login_shell,
)


def run_ssh_command(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    capture_output: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Execute a command on Bridge via SSH ProxyCommand."""
    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config)
    ssh_cmd = _build_ssh_base_args(bridge=bridge, proxy_cmd=proxy_cmd)
    ssh_cmd.append(_wrap_command_in_login_shell(command))

    return subprocess.run(
        ssh_cmd,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=check,
    )


__all__ = ["run_ssh_command"]
