"""SSH command execution helpers (ProxyCommand mode).

The implementation is split across smaller modules; this file re-exports the public API to keep
historical import paths stable.
"""

from __future__ import annotations

from inspire.cli.utils.tunnel_ssh_exec_args import get_ssh_command_args  # noqa: F401
from inspire.cli.utils.tunnel_ssh_exec_run import run_ssh_command  # noqa: F401
from inspire.cli.utils.tunnel_ssh_exec_stream import run_ssh_command_streaming  # noqa: F401

__all__ = [
    "get_ssh_command_args",
    "run_ssh_command",
    "run_ssh_command_streaming",
]
