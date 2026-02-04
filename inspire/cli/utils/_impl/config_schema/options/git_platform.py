"""Config options: Git Platform."""

from __future__ import annotations

from inspire.cli.utils.config_schema_models import ConfigOption

GIT_PLATFORM_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSP_GIT_PLATFORM",
        toml_key="git.platform",
        field_name="git_platform",
        description="Git platform to use: 'gitea' or 'github' (default: gitea)",
        default="gitea",
        category="Git Platform",
        scope="project",
    ),
]
