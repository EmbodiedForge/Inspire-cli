"""Config options: Gitea, GitHub, and Git Platform."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption, _parse_int

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
