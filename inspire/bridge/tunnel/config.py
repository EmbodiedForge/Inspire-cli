"""Tunnel configuration file management."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .models import BridgeProfile, TunnelConfig, DEFAULT_SSH_USER


def load_tunnel_config(
    config_dir: Optional[Path] = None,
    account: Optional[str] = None,
) -> TunnelConfig:
    """Load tunnel configuration from ~/.inspire/bridges[-{account}].json.

    Account resolution order:
      1. *account* parameter (explicit)
      2. ``INSPIRE_ACCOUNT`` environment variable
      3. ``None`` — uses legacy ``bridges.json``

    When an account is resolved, reads ``bridges-{account}.json``.  If that
    file does not exist, falls back to reading the legacy ``bridges.json``
    (but saves will always target the account-specific file).
    """
    resolved_account = account or os.environ.get("INSPIRE_ACCOUNT") or None

    config = TunnelConfig(account=resolved_account)
    if config_dir:
        config.config_dir = config_dir

    config.config_dir.mkdir(parents=True, exist_ok=True)

    # Determine which file to read.  Prefer the account-specific file; fall
    # back to the legacy ``bridges.json`` if the account file doesn't exist.
    read_path = config.config_file
    if not read_path.exists() and resolved_account:
        legacy = config.config_dir / "bridges.json"
        if legacy.exists():
            read_path = legacy

    # Try new JSON format first
    if read_path.exists():
        try:
            with open(read_path) as f:
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
    """Save tunnel configuration to ~/.inspire/bridges[-{account}].json."""
    config.config_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "default": config.default_bridge,
        "bridges": [p.to_dict() for p in config.bridges.values()],
    }

    with open(config.config_file, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
