"""Configuration façade for Inspire CLI.

The implementation is split across smaller modules (models, env parsing, loading), but this file
re-exports the public API to keep import paths stable.
"""

from __future__ import annotations

from inspire.cli.utils.config_env import _parse_denylist, _parse_remote_timeout, build_env_exports
from inspire.cli.utils.config_models import (
    CONFIG_FILENAME,
    PROJECT_CONFIG_DIR,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    Config,
    ConfigError,
)

__all__ = [
    "CONFIG_FILENAME",
    "PROJECT_CONFIG_DIR",
    "Config",
    "ConfigError",
    "SOURCE_DEFAULT",
    "SOURCE_GLOBAL",
    "SOURCE_PROJECT",
    "SOURCE_ENV",
    "_parse_denylist",
    "_parse_remote_timeout",
    "build_env_exports",
]
