"""Config loading and merging for Inspire CLI (façade)."""

from __future__ import annotations

from inspire.cli.utils.config_loader_env import (  # noqa: F401
    config_from_env,
    config_from_env_for_sync,
)
from inspire.cli.utils.config_loader_merge import (  # noqa: F401
    config_from_files_and_env,
    get_config_paths,
)
from inspire.cli.utils.config_loader_toml import (  # noqa: F401
    _find_project_config,
    _flatten_toml,
    _load_toml,
    _toml_key_to_field,
)

__all__ = [
    "_find_project_config",
    "_flatten_toml",
    "_load_toml",
    "_toml_key_to_field",
    "config_from_env",
    "config_from_env_for_sync",
    "config_from_files_and_env",
    "get_config_paths",
]
