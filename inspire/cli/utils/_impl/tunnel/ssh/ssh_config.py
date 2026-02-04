"""SSH config generation for rtunnel ProxyCommand access."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from inspire.cli.utils.tunnel_models import BridgeProfile, TunnelConfig
from inspire.cli.utils.tunnel_rtunnel import _ensure_rtunnel_binary


def generate_ssh_config(
    bridge: BridgeProfile,
    rtunnel_path: Path,
    host_alias: Optional[str] = None,
) -> str:
    """Generate SSH config for ProxyCommand mode.

    Args:
        bridge: Bridge profile
        rtunnel_path: Path to rtunnel binary
        host_alias: SSH host alias to use (defaults to bridge name)

    Returns:
        SSH config string to add to ~/.ssh/config
    """
    if host_alias is None:
        host_alias = bridge.name

    # Convert https:// URL to wss:// for websocket
    proxy_url = bridge.proxy_url
    if proxy_url.startswith("https://"):
        ws_url = "wss://" + proxy_url[8:]
    elif proxy_url.startswith("http://"):
        ws_url = "ws://" + proxy_url[7:]
    else:
        ws_url = proxy_url

    ssh_config = f"""Host {host_alias}
    HostName localhost
    User {bridge.ssh_user}
    Port {bridge.ssh_port}
    ProxyCommand {rtunnel_path} {ws_url} stdio://%h:%p
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR"""

    return ssh_config


def generate_all_ssh_configs(config: TunnelConfig) -> str:
    """Generate SSH config for all bridges.

    Args:
        config: Tunnel configuration with all bridges

    Returns:
        SSH config string for all bridges
    """
    if not config.bridges:
        return ""

    rtunnel_path = _ensure_rtunnel_binary(config)
    configs = []
    for bridge in config.list_bridges():
        configs.append(generate_ssh_config(bridge, rtunnel_path))

    return "\n\n".join(configs)


def install_ssh_config(ssh_config: str, host_alias: str) -> dict:
    """Install SSH config to ~/.ssh/config.

    Args:
        ssh_config: SSH config block to add
        host_alias: Host alias to look for (for updating existing entries)

    Returns:
        Dict with keys:
        - success: bool
        - updated: bool (True if existing entry was updated)
        - error: Optional[str]
    """
    import re

    ssh_config_path = Path.home() / ".ssh" / "config"
    ssh_config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    existing_content = ""
    if ssh_config_path.exists():
        existing_content = ssh_config_path.read_text()

    # Check if host alias already exists
    # Match "Host <alias>" at start of line, possibly with other hosts on same line
    host_pattern = rf"^Host\s+.*?\b{re.escape(host_alias)}\b.*$"
    match = re.search(host_pattern, existing_content, re.MULTILINE)

    if match:
        # Find the full block to replace (from Host line to next Host line or end)
        block_pattern = rf"(^Host\s+.*?\b{re.escape(host_alias)}\b.*$)((?:\n(?!Host\s).*)*)"
        new_content = re.sub(block_pattern, ssh_config, existing_content, flags=re.MULTILINE)

        ssh_config_path.write_text(new_content)
        return {"success": True, "updated": True, "error": None}
    else:
        # Append new entry
        if existing_content and not existing_content.endswith("\n"):
            existing_content += "\n"
        if existing_content:
            existing_content += "\n"

        ssh_config_path.write_text(existing_content + ssh_config + "\n")
        return {"success": True, "updated": False, "error": None}
