"""Config options: Gitea."""

from __future__ import annotations

from inspire.cli.utils.config_schema_models import ConfigOption, _parse_int

GITEA_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSP_GITEA_SERVER",
        toml_key="gitea.server",
        field_name="gitea_server",
        description="Gitea server URL",
        default="https://codeberg.org",
        category="Gitea",
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_GITEA_REPO",
        toml_key="gitea.repo",
        field_name="gitea_repo",
        description="Gitea repository (owner/repo format)",
        default=None,
        category="Gitea",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITEA_TOKEN",
        toml_key="gitea.token",
        field_name="gitea_token",
        description="Gitea personal access token (use env var)",
        default=None,
        category="Gitea",
        secret=True,
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_GITEA_LOG_WORKFLOW",
        toml_key="gitea.log_workflow",
        field_name="gitea_log_workflow",
        description="Workflow filename for retrieving logs",
        default="retrieve_job_log.yml",
        category="Gitea",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITEA_SYNC_WORKFLOW",
        toml_key="gitea.sync_workflow",
        field_name="gitea_sync_workflow",
        description="Workflow filename for code sync",
        default="sync_code.yml",
        category="Gitea",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITEA_BRIDGE_WORKFLOW",
        toml_key="gitea.bridge_workflow",
        field_name="gitea_bridge_workflow",
        description="Workflow filename for bridge execution",
        default="run_bridge_action.yml",
        category="Gitea",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_REMOTE_TIMEOUT",
        toml_key="gitea.remote_timeout",
        field_name="remote_timeout",
        description="Max time to wait for remote artifact (seconds)",
        default=90,
        category="Gitea",
        parser=_parse_int,
        scope="project",
    ),
]
