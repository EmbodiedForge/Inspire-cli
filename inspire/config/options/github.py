"""Config options: GitHub."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption

GITHUB_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSP_GITHUB_SERVER",
        toml_key="github.server",
        field_name="github_server",
        description="GitHub server URL",
        default="https://github.com",
        category="GitHub",
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_REPO",
        toml_key="github.repo",
        field_name="github_repo",
        description="GitHub repository (owner/repo format)",
        default=None,
        category="GitHub",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_TOKEN",
        toml_key="github.token",
        field_name="github_token",
        description="GitHub personal access token (falls back to GITHUB_TOKEN)",
        default=None,
        category="GitHub",
        secret=True,
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_LOG_WORKFLOW",
        toml_key="github.log_workflow",
        field_name="github_log_workflow",
        description="Workflow filename for retrieving logs (GitHub)",
        default="retrieve_job_log.yml",
        category="GitHub",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_SYNC_WORKFLOW",
        toml_key="github.sync_workflow",
        field_name="github_sync_workflow",
        description="Workflow filename for code sync (GitHub)",
        default="sync_code.yml",
        category="GitHub",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_BRIDGE_WORKFLOW",
        toml_key="github.bridge_workflow",
        field_name="github_bridge_workflow",
        description="Workflow filename for bridge execution (GitHub)",
        default="run_bridge_action.yml",
        category="GitHub",
        scope="project",
    ),
]
