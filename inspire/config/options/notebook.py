"""Config options: Notebook."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption

NOTEBOOK_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_RESOURCE",
        toml_key="notebook.resource",
        field_name="notebook_resource",
        description="Default resource for notebooks",
        default="1xH200",
        category="Notebook",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_IMAGE",
        toml_key="notebook.image",
        field_name="notebook_image",
        description="Default Docker image for notebooks",
        default=None,
        category="Notebook",
        scope="project",
    ),
]
