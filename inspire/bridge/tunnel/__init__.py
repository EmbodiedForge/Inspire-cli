"""SSH tunnel utilities (ProxyCommand + rtunnel).

This package contains the tunnel domain logic used by the CLI (tunnel management, ssh execution,
and optional ssh-config generation).
"""

from __future__ import annotations

from .config import load_tunnel_config, save_tunnel_config
from .models import (
    BridgeNotFoundError,
    BridgeProfile,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    TunnelConfig,
    TunnelError,
    TunnelNotAvailableError,
    has_internet_for_gpu_type,
)
from .rtunnel import (
    DEFAULT_RTUNNEL_DOWNLOAD_URL,
    _ensure_rtunnel_binary,
    _get_rtunnel_download_url,
    get_rtunnel_path,
)
from .ssh.connection import _test_ssh_connection, is_tunnel_available
from .ssh.proxy import _get_proxy_command
from .ssh.ssh_config import (
    generate_all_ssh_configs,
    generate_ssh_config,
    install_ssh_config,
)
from .ssh.status import get_tunnel_status
from .ssh_exec.args import get_ssh_command_args
from .ssh_exec.run import run_ssh_command
from .ssh_exec.stream import run_ssh_command_streaming
from .sync import sync_via_ssh

__all__ = [
    # Models / errors
    "BridgeNotFoundError",
    "BridgeProfile",
    "DEFAULT_SSH_PORT",
    "DEFAULT_SSH_USER",
    "TunnelConfig",
    "TunnelError",
    "TunnelNotAvailableError",
    "has_internet_for_gpu_type",
    # Config
    "load_tunnel_config",
    "save_tunnel_config",
    # rtunnel
    "DEFAULT_RTUNNEL_DOWNLOAD_URL",
    "_ensure_rtunnel_binary",
    "_get_rtunnel_download_url",
    "get_rtunnel_path",
    # SSH helpers
    "_get_proxy_command",
    "_test_ssh_connection",
    "get_ssh_command_args",
    "get_tunnel_status",
    "is_tunnel_available",
    "run_ssh_command",
    "run_ssh_command_streaming",
    # ssh-config
    "generate_all_ssh_configs",
    "generate_ssh_config",
    "install_ssh_config",
    # Sync
    "sync_via_ssh",
]
