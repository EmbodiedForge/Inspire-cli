"""Config options: Mirrors."""

from __future__ import annotations

from inspire.cli.utils.config_schema_models import ConfigOption

MIRRORS_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_APT_MIRROR_URL",
        toml_key="mirrors.apt_mirror_url",
        field_name="apt_mirror_url",
        description="APT mirror URL for package installation",
        default=None,
        category="Mirrors",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PIP_INDEX_URL",
        toml_key="mirrors.pip_index_url",
        field_name="pip_index_url",
        description="PyPI mirror URL for Python packages",
        default=None,
        category="Mirrors",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PIP_TRUSTED_HOST",
        toml_key="mirrors.pip_trusted_host",
        field_name="pip_trusted_host",
        description="Trusted host for pip (when using self-signed certs)",
        default=None,
        category="Mirrors",
        scope="global",
    ),
]
