"""Configuration schema for Inspire CLI.

Defines all environment variables and TOML configuration keys with metadata
for documentation, validation, and config file generation.
"""

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ConfigOption:
    """A single configuration option with metadata.

    Attributes:
        env_var: Environment variable name
        toml_key: TOML configuration key (e.g., "auth.username")
        field_name: Config dataclass field name (e.g., "username")
        description: Human-readable description
        default: Default value (None if required)
        category: Configuration category for grouping
        secret: If True, value should be hidden in output
        parser: Optional function to parse string value to correct type
        validator: Optional function to validate the value
        scope: Configuration scope - "global" for user/machine-specific settings,
               "project" for per-codebase settings
    """

    env_var: str
    toml_key: str
    field_name: str
    description: str
    default: Any | None
    category: str
    secret: bool = False
    parser: Callable[[str], Any] | None = None
    validator: Callable[[Any], bool] | None = None
    scope: str = "project"


# Parser functions
def _parse_int(value: str) -> int:
    """Parse string to integer."""
    return int(value)


def _parse_float(value: str) -> float:
    """Parse string to float."""
    return float(value)


def _parse_bool(value: str) -> bool:
    """Parse string to boolean."""
    return value.lower() in ("1", "true", "yes", "on")


def _parse_list(value: str) -> list[str]:
    """Parse comma or newline separated list."""
    if not value:
        return []
    parts = []
    for raw in value.replace("\r", "").split("\n"):
        for chunk in raw.split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


# All configuration options organized by category
CONFIG_OPTIONS: list[ConfigOption] = [
    # Authentication (global scope - user identity)
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
    # API Settings (global scope - platform settings)
    ConfigOption(
        env_var="INSPIRE_BASE_URL",
        toml_key="api.base_url",
        field_name="base_url",
        description="API base URL",
        default="https://api.example.com",
        category="API",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_TIMEOUT",
        toml_key="api.timeout",
        field_name="timeout",
        description="API timeout in seconds",
        default=30,
        category="API",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_MAX_RETRIES",
        toml_key="api.max_retries",
        field_name="max_retries",
        description="Maximum API retry attempts",
        default=3,
        category="API",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_RETRY_DELAY",
        toml_key="api.retry_delay",
        field_name="retry_delay",
        description="Delay between retries in seconds",
        default=1.0,
        category="API",
        parser=_parse_float,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_SKIP_SSL_VERIFY",
        toml_key="api.skip_ssl_verify",
        field_name="skip_ssl_verify",
        description="Skip SSL certificate verification (not recommended)",
        default=False,
        category="API",
        parser=_parse_bool,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_FORCE_PROXY",
        toml_key="api.force_proxy",
        field_name="force_proxy",
        description="Force use of proxy settings",
        default=False,
        category="API",
        parser=_parse_bool,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_OPENAPI_PREFIX",
        toml_key="api.openapi_prefix",
        field_name="openapi_prefix",
        description="OpenAPI endpoint path prefix",
        default=None,
        category="API",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_BROWSER_API_PREFIX",
        toml_key="api.browser_api_prefix",
        field_name="browser_api_prefix",
        description="Browser API endpoint path prefix",
        default=None,
        category="API",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_AUTH_ENDPOINT",
        toml_key="api.auth_endpoint",
        field_name="auth_endpoint",
        description="Authentication endpoint path",
        default=None,
        category="API",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_DOCKER_REGISTRY",
        toml_key="api.docker_registry",
        field_name="docker_registry",
        description="Docker registry hostname",
        default=None,
        category="API",
        scope="global",
    ),
    # Paths (mixed scope)
    ConfigOption(
        env_var="INSPIRE_TARGET_DIR",
        toml_key="paths.target_dir",
        field_name="target_dir",
        description="Target directory on Bridge shared filesystem",
        default=None,
        category="Paths",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_LOG_PATTERN",
        toml_key="paths.log_pattern",
        field_name="log_pattern",
        description="Log file glob pattern",
        default="training_master_*.log",
        category="Paths",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_JOB_CACHE",
        toml_key="paths.job_cache",
        field_name="job_cache_path",
        description="Local job cache file path",
        default="~/.inspire/jobs.json",
        category="Paths",
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_LOG_CACHE_DIR",
        toml_key="paths.log_cache_dir",
        field_name="log_cache_dir",
        description="Cache directory for remote logs",
        default="~/.inspire/logs",
        category="Paths",
        scope="global",
    ),
    # Git Platform (mixed scope)
    ConfigOption(
        env_var="INSP_GIT_PLATFORM",
        toml_key="git.platform",
        field_name="git_platform",
        description="Git platform to use: 'gitea' or 'github' (default: gitea)",
        default="gitea",
        category="Git Platform",
        scope="project",
    ),
    # Gitea / Bridge (mixed scope)
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
    # GitHub (mixed scope)
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
    # Sync Settings (project scope)
    ConfigOption(
        env_var="INSPIRE_DEFAULT_REMOTE",
        toml_key="sync.default_remote",
        field_name="default_remote",
        description="Default git remote name",
        default="origin",
        category="Sync",
        scope="project",
    ),
    # Bridge Settings (project scope)
    ConfigOption(
        env_var="INSPIRE_BRIDGE_ACTION_TIMEOUT",
        toml_key="bridge.action_timeout",
        field_name="bridge_action_timeout",
        description="Bridge action timeout in seconds",
        default=300,
        category="Bridge",
        parser=_parse_int,
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_BRIDGE_DENYLIST",
        toml_key="bridge.denylist",
        field_name="bridge_action_denylist",
        description="Glob patterns to block from sync (comma/newline separated)",
        default=[],
        category="Bridge",
        parser=_parse_list,
        scope="project",
    ),
    # Job Settings (project scope)
    ConfigOption(
        env_var="INSP_PRIORITY",
        toml_key="job.priority",
        field_name="job_priority",
        description="Default job priority (1-10)",
        default=6,
        category="Job",
        parser=_parse_int,
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_IMAGE",
        toml_key="job.image",
        field_name="job_image",
        description="Default Docker image for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_PROJECT_ID",
        toml_key="job.project_id",
        field_name="job_project_id",
        description="Default project ID for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_ID",
        toml_key="job.workspace_id",
        field_name="job_workspace_id",
        description="Default workspace ID for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
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
    ConfigOption(
        env_var="INSPIRE_SHM_SIZE",
        toml_key="job.shm_size",
        field_name="shm_size",
        description="Default shared memory size in GB for jobs",
        default=None,
        category="Job",
        parser=_parse_int,
        scope="project",
    ),
    # Notebook Settings (project scope)
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
    # SSH / Tunnel Settings (global scope - local tool paths)
    ConfigOption(
        env_var="INSPIRE_RTUNNEL_BIN",
        toml_key="ssh.rtunnel_bin",
        field_name="rtunnel_bin",
        description="Path to rtunnel binary",
        default=None,
        category="SSH",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_SSHD_DEB_DIR",
        toml_key="ssh.sshd_deb_dir",
        field_name="sshd_deb_dir",
        description="Directory containing sshd deb package",
        default=None,
        category="SSH",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_DROPBEAR_DEB_DIR",
        toml_key="ssh.dropbear_deb_dir",
        field_name="dropbear_deb_dir",
        description="Directory containing dropbear deb package",
        default=None,
        category="SSH",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_SETUP_SCRIPT",
        toml_key="ssh.setup_script",
        field_name="setup_script",
        description="Path to SSH setup script on the cluster",
        default=None,
        category="SSH",
        scope="global",
        secret=True,
    ),
    ConfigOption(
        env_var="INSPIRE_RTUNNEL_DOWNLOAD_URL",
        toml_key="ssh.rtunnel_download_url",
        field_name="rtunnel_download_url",
        description="Download URL for rtunnel binary",
        default="https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz",
        category="SSH",
        scope="global",
    ),
    # Mirror Settings (global scope - network-specific)
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
    # Tunnel retry settings (global scope - local connectivity behavior)
    ConfigOption(
        env_var="INSPIRE_TUNNEL_RETRIES",
        toml_key="tunnel.retries",
        field_name="tunnel_retries",
        description="SSH tunnel connection retries",
        default=3,
        category="Tunnel",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_TUNNEL_RETRY_PAUSE",
        toml_key="tunnel.retry_pause",
        field_name="tunnel_retry_pause",
        description="Seconds to wait between SSH tunnel retries",
        default=2.0,
        category="Tunnel",
        parser=_parse_float,
        scope="global",
    ),
    # Compute groups (loaded from config.toml [[compute_groups]] sections - not an env var)
]


# Category order for display
CATEGORY_ORDER = [
    "Authentication",
    "API",
    "Paths",
    "Git Platform",
    "Gitea",
    "GitHub",
    "Sync",
    "Bridge",
    "Workspaces",
    "Job",
    "Notebook",
    "SSH",
    "Tunnel",
    "Mirrors",
]


def get_options_by_category(category: str) -> list[ConfigOption]:
    """Get all configuration options for a category."""
    return [opt for opt in CONFIG_OPTIONS if opt.category == category]


def get_option_by_env(env_var: str) -> ConfigOption | None:
    """Get configuration option by environment variable name."""
    for opt in CONFIG_OPTIONS:
        if opt.env_var == env_var:
            return opt
    return None


def get_option_by_toml(toml_key: str) -> ConfigOption | None:
    """Get configuration option by TOML key."""
    for opt in CONFIG_OPTIONS:
        if opt.toml_key == toml_key:
            return opt
    return None


def get_categories() -> list[str]:
    """Get all unique categories in order."""
    return [cat for cat in CATEGORY_ORDER if any(opt.category == cat for opt in CONFIG_OPTIONS)]


def get_required_options() -> list[ConfigOption]:
    """Get all required configuration options (no default)."""
    return [opt for opt in CONFIG_OPTIONS if opt.default is None]


def get_secret_options() -> list[ConfigOption]:
    """Get all secret configuration options."""
    return [opt for opt in CONFIG_OPTIONS if opt.secret]


def get_options_by_scope(scope: str) -> list[ConfigOption]:
    """Get all configuration options for a given scope.

    Args:
        scope: Either "global" or "project"

    Returns:
        List of ConfigOption with matching scope
    """
    return [opt for opt in CONFIG_OPTIONS if opt.scope == scope]


def parse_value(option: ConfigOption, value: str) -> Any:
    """Parse a string value based on the option's parser."""
    if option.parser:
        try:
            return option.parser(value)
        except (ValueError, TypeError):
            return value
    return value
