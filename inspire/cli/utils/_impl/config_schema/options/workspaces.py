"""Config options: Workspaces."""

from __future__ import annotations

from inspire.cli.utils.config_schema_models import ConfigOption

WORKSPACES_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_CPU_ID",
        toml_key="workspaces.cpu",
        field_name="workspace_cpu_id",
        description="Workspace ID for CPU workloads (default workspace)",
        default=None,
        category="Workspaces",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_GPU_ID",
        toml_key="workspaces.gpu",
        field_name="workspace_gpu_id",
        description="Workspace ID for GPU workloads (H100/H200)",
        default=None,
        category="Workspaces",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_INTERNET_ID",
        toml_key="workspaces.internet",
        field_name="workspace_internet_id",
        description="Workspace ID for internet-enabled workloads (e.g. RTX 4090)",
        default=None,
        category="Workspaces",
        scope="project",
    ),
]
