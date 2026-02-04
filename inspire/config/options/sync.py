"""Config options: Sync."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption

SYNC_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_DEFAULT_REMOTE",
        toml_key="sync.default_remote",
        field_name="default_remote",
        description="Default git remote name",
        default="origin",
        category="Sync",
        scope="project",
    ),
]
