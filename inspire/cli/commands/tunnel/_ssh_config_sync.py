"""Helpers for refreshing installed Inspire SSH config after tunnel mutations."""

from __future__ import annotations

from inspire.bridge.tunnel import TunnelConfig, has_installed_ssh_config, install_all_ssh_configs


def sync_installed_ssh_config(config: TunnelConfig) -> tuple[bool, str | None]:
    """Refresh ~/.ssh/config when a managed Inspire block is already installed."""
    try:
        if not has_installed_ssh_config():
            return False, None
        result = install_all_ssh_configs(config)
        if result.get("success"):
            return True, None
        return False, result.get("error") or "Failed to update ~/.ssh/config"
    except Exception as error:
        return False, str(error)
