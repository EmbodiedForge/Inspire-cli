"""SSH tunnel utility façade.

Historically all tunnel logic lived in this module. The implementation is now split into smaller
modules; this file re-exports the public API to keep import paths stable.
"""

from __future__ import annotations

from inspire.cli.utils.tunnel_config import load_tunnel_config, save_tunnel_config
from inspire.cli.utils.tunnel_models import (
    BridgeNotFoundError,
    BridgeProfile,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    TunnelConfig,
    TunnelError,
    TunnelNotAvailableError,
    has_internet_for_gpu_type,
)
from inspire.cli.utils.tunnel_rtunnel import (
    DEFAULT_RTUNNEL_DOWNLOAD_URL,
    _ensure_rtunnel_binary,
    _get_rtunnel_download_url,
    get_rtunnel_path,
)
from inspire.cli.utils.tunnel_ssh import (
    _get_proxy_command,
    _test_ssh_connection,
    get_ssh_command_args,
    get_tunnel_status,
    is_tunnel_available,
    run_ssh_command,
    run_ssh_command_streaming,
)
from inspire.cli.utils._impl.tunnel.ssh.ssh_config import (
    generate_all_ssh_configs,
    generate_ssh_config,
    install_ssh_config,
)
from inspire.cli.utils.tunnel_sync import sync_via_ssh

__all__ = [
    "BridgeNotFoundError",
    "BridgeProfile",
    "DEFAULT_SSH_PORT",
    "DEFAULT_SSH_USER",
    "TunnelConfig",
    "TunnelError",
    "TunnelNotAvailableError",
    "has_internet_for_gpu_type",
    "load_tunnel_config",
    "save_tunnel_config",
    "DEFAULT_RTUNNEL_DOWNLOAD_URL",
    "_ensure_rtunnel_binary",
    "_get_rtunnel_download_url",
    "get_rtunnel_path",
    "_get_proxy_command",
    "_test_ssh_connection",
    "get_ssh_command_args",
    "get_tunnel_status",
    "is_tunnel_available",
    "run_ssh_command",
    "run_ssh_command_streaming",
    "generate_all_ssh_configs",
    "generate_ssh_config",
    "install_ssh_config",
    "sync_via_ssh",
]
