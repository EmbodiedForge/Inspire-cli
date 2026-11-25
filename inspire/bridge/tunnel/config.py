"""Tunnel configuration file management."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from .models import BridgeProfile, TunnelConfig, DEFAULT_SSH_USER
from inspire.config.permissions import harden_state_permissions  # noqa: F401

logger = logging.getLogger(__name__)


def _resolve_account_scope_from_config() -> tuple[Optional[str], bool]:
    """Return the state key and whether it came from an Account Catalog alias."""
    try:
        from inspire.config import Config, ConfigError

        config, _ = Config.from_files_and_env(require_credentials=False)
    except (ImportError, ConfigError, OSError, ValueError, TypeError) as error:
        logger.debug("Unable to resolve username from layered config: %s", error)
        return None, False

    account_alias = str(getattr(config, "account_alias", "") or "").strip()
    if account_alias:
        return account_alias, True
    username = str(getattr(config, "username", "") or "").strip()
    return username or None, False


def _resolve_tunnel_scope(account: Optional[str]) -> tuple[Optional[str], bool]:
    """Resolve the bridge state key and whether strict alias isolation applies."""
    explicit = (account or "").strip()
    if explicit:
        return explicit, True

    bridge_account = (os.environ.get("INSPIRE_BRIDGE_ACCOUNT") or "").strip()
    if bridge_account:
        return bridge_account, False

    config_account, alias_backed = _resolve_account_scope_from_config()
    if config_account:
        return config_account, alias_backed

    env_username = (os.environ.get("INSPIRE_USERNAME") or "").strip()
    if env_username:
        return env_username, False

    return None, False


def _resolve_tunnel_account(account: Optional[str]) -> Optional[str]:
    """Resolve the bridge account key used for bridges-<account>.json."""
    resolved_account, _ = _resolve_tunnel_scope(account)
    return resolved_account


def _candidate_config_paths(
    config_dir: Path,
    resolved_account: Optional[str],
    *,
    include_legacy: bool = True,
) -> list[Path]:
    """Build config read candidates in precedence order."""
    candidates: list[Path] = []

    if resolved_account:
        candidates.append(config_dir / f"bridges-{resolved_account}.json")

    if include_legacy or not resolved_account:
        candidates.append(config_dir / "bridges.json")

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def load_tunnel_config(
    config_dir: Optional[Path] = None,
    account: Optional[str] = None,
) -> TunnelConfig:
    """Load tunnel configuration from ~/.inspire/bridges[-{account}].json.

    Account resolution order:
      1. *account* parameter (explicit)
      2. ``INSPIRE_BRIDGE_ACCOUNT`` environment variable
      3. resolved config alias, or username for explicit legacy config
      4. ``INSPIRE_USERNAME`` when layered config cannot be loaded
      5. ``None`` — uses legacy ``bridges.json``

    Catalog aliases read only their alias-scoped file. Legacy username and
    ``INSPIRE_BRIDGE_ACCOUNT`` scopes retain the shared-file fallback for
    compatibility. Saves target the resolved account file when available.
    """
    resolved_account, alias_backed = _resolve_tunnel_scope(account)

    config = TunnelConfig(account=resolved_account)
    if config_dir:
        config.config_dir = config_dir

    config.config_dir.mkdir(parents=True, exist_ok=True)
    # Self-heal state left group/world-readable by older releases; see the helper docstring.
    harden_state_permissions(config.config_dir)

    preferred_defaults: list[str] = []

    # Alias-backed state is isolated; legacy state keeps its shared fallback.
    for read_path in _candidate_config_paths(
        config.config_dir,
        resolved_account,
        include_legacy=not alias_backed,
    ):
        if not read_path.exists():
            continue
        try:
            with open(read_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        default_name = str(data.get("default") or "").strip()
        if default_name:
            preferred_defaults.append(default_name)

        for bridge_data in data.get("bridges", []):
            try:
                profile = BridgeProfile.from_dict(bridge_data)
            except (KeyError, TypeError):
                continue
            if profile.name in config.bridges:
                continue
            config.bridges[profile.name] = profile

    for default_name in preferred_defaults:
        if default_name in config.bridges:
            config.default_bridge = default_name
            break

    if config.default_bridge is None and config.bridges:
        config.default_bridge = next(iter(config.bridges.keys()))

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

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=config.config_dir,
            prefix=f".{config.config_file.name}.",
            delete=False,
        ) as f:
            tmp_path = Path(f.name)
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config.config_file)
        os.chmod(config.config_file, 0o600)
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
