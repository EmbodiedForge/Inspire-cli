"""Config options: Authentication."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption

AUTH_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_USERNAME",
        toml_key="auth.username",
        field_name="username",
        description="Platform username",
        default=None,
        category="Authentication",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PASSWORD",
        toml_key="auth.password",
        field_name="password",
        description="Platform password (use env var for security)",
        default=None,
        category="Authentication",
        secret=True,
        scope="global",
    ),
]
