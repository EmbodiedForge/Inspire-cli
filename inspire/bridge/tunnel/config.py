"""Tunnel configuration file management."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .models import BridgeProfile, TunnelConfig, DEFAULT_SSH_USER


def load_tunnel_config(config_dir: Optional[Path] = None) -> TunnelConfig:
    """Load tunnel configuration from ~/.inspire/bridges.json."""
    config = TunnelConfig()
    if config_dir:
        config.config_dir = config_dir

    config.config_dir.mkdir(parents=True, exist_ok=True)

    # Try new JSON format first
    if config.config_file.exists():
        try:
            with open(config.config_file) as f:
                data = json.load(f)
                config.default_bridge = data.get("default")
                for bridge_data in data.get("bridges", []):
                    profile = BridgeProfile.from_dict(bridge_data)
                    config.bridges[profile.name] = profile
        except (json.JSONDecodeError, KeyError):
            pass

    # Migrate from old format if new format is empty
    old_config_file = config.config_dir / "tunnel.conf"
    if not config.bridges and old_config_file.exists():
        proxy_url = None
        ssh_user = DEFAULT_SSH_USER
        with open(old_config_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key == "PROXY_URL":
                        proxy_url = value
                    elif key == "SSH_USER":
                        ssh_user = value

        if proxy_url:
            # Create a default bridge from old config
            profile = BridgeProfile(
                name="default",
                proxy_url=proxy_url,
                ssh_user=ssh_user,
            )
            config.add_bridge(profile)
            # Save in new format
            save_tunnel_config(config)

    return config


def save_tunnel_config(config: TunnelConfig) -> None:
    """Save tunnel configuration to ~/.inspire/bridges.json."""
    config.config_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "default": config.default_bridge,
        "bridges": [p.to_dict() for p in config.bridges.values()],
    }

    with open(config.config_file, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
