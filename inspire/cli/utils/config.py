"""Configuration management for Inspire CLI.

Reads configuration from environment variables and TOML config files with sensible defaults.

Config precedence (lowest to highest):
    Hardcoded defaults < Global config.toml < Project config.toml < Environment variables
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ImportError:
    # Python < 3.11 fallback
    import tomli as tomllib

from inspire.cli.utils.config_schema import (
    CONFIG_OPTIONS,
    ConfigOption,
    get_option_by_toml,
    parse_value as parse_schema_value,
    get_categories,
    CATEGORY_ORDER,
    _parse_bool,
)

# Config file paths
CONFIG_FILENAME = "config.toml"
PROJECT_CONFIG_DIR = ".inspire"  # ./.inspire/config.toml


class ConfigError(Exception):
    """Configuration error - missing or invalid settings."""

    pass


# Source tracking for config values
SOURCE_DEFAULT = "default"
SOURCE_GLOBAL = "global"
SOURCE_PROJECT = "project"
SOURCE_ENV = "env"


def _parse_remote_timeout(value: str) -> int:
    """Parse INSP_REMOTE_TIMEOUT environment variable.

    Args:
        value: String value to parse

    Returns:
        Integer seconds

    Raises:
        ConfigError: If value is not a valid integer
    """
    try:
        timeout = int(value)
        if timeout < 5:
            # Warn but allow small values for testing
            pass
        return timeout
    except ValueError:
        raise ConfigError(
            "Invalid INSP_REMOTE_TIMEOUT value. It must be an integer number of seconds."
        )


def _parse_denylist(value: Optional[str]) -> list[str]:
    """Parse denylist from env (comma or newline separated)."""

    if not value:
        return []
    parts = []
    for raw in value.replace("\r", "").split("\n"):
        for chunk in raw.split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


@dataclass
class Config:
    """Inspire CLI configuration.

    All configuration is read from environment variables:

    **Platform API (required for job commands):**
    - INSPIRE_USERNAME: Platform username
    - INSPIRE_PASSWORD: Platform password
    - INSPIRE_BASE_URL: API base URL (default: https://qz.sii.edu.cn)

    **Target directory (unified for all Bridge operations):**
    - INSPIRE_TARGET_DIR: Shared filesystem path on Bridge (e.g., /shared/EBM_dev)
      - Used for: code sync, bridge exec, job logs
      - All commands run relative to this directory

    **Log settings:**
    - INSPIRE_LOG_PATTERN: Log file glob pattern (default: training_master_*.log)

    **Gitea bridge (required for sync, bridge exec, remote logs):**
    - INSP_GITEA_REPO: Gitea repo as 'owner/repo'
    - INSP_GITEA_TOKEN: Gitea Personal Access Token
    - INSP_GITEA_SERVER: Gitea server URL (e.g., https://gitea.example.com)
    - INSP_LOG_CACHE_DIR: Cache directory for remote logs (default: ~/.inspire/logs)
    - INSP_REMOTE_TIMEOUT: Max time to wait for artifact (seconds, default: 90)

    **Job cache (optional):**
    - INSPIRE_JOB_CACHE: Local job cache location (default: ~/.inspire/jobs.json)

    **API tuning (optional):**
    - INSPIRE_TIMEOUT: API timeout in seconds (default: 30)
    - INSPIRE_MAX_RETRIES: Max API retries (default: 3)
    - INSPIRE_RETRY_DELAY: Retry delay in seconds (default: 1.0)

    **Bridge exec settings:**
    - INSPIRE_BRIDGE_ACTION_TIMEOUT: Timeout in seconds (default: 300)
    - INSPIRE_BRIDGE_DENYLIST: Glob patterns to block (comma/newline separated)
    """

    # Required
    username: str
    password: str

    # Optional with defaults
    base_url: str = "https://qz.sii.edu.cn"
    target_dir: Optional[str] = None  # INSPIRE_TARGET_DIR - unified for all Bridge operations
    log_pattern: str = "training_master_*.log"
    job_cache_path: str = "~/.inspire/jobs.json"

    # API settings
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0

    # Gitea / remote log settings
    gitea_repo: Optional[str] = None
    gitea_token: Optional[str] = None
    gitea_server: str = "https://codeberg.org"
    gitea_log_workflow: str = "retrieve_job_log.yml"
    gitea_sync_workflow: str = "sync_code.yml"
    gitea_bridge_workflow: str = "run_bridge_action.yml"
    log_cache_dir: str = "~/.inspire/logs"
    remote_timeout: int = 90

    # Sync settings
    default_remote: str = "origin"

    # Bridge action settings
    bridge_action_timeout: int = 300
    bridge_action_denylist: list[str] = field(default_factory=list)

    # API settings (additional)
    skip_ssl_verify: bool = False
    force_proxy: bool = False

    # API path prefixes (None = use code defaults)
    openapi_prefix: Optional[str] = None
    browser_api_prefix: Optional[str] = None
    auth_endpoint: Optional[str] = None
    docker_registry: Optional[str] = None

    # Job settings
    job_priority: int = 6
    job_image: Optional[str] = None
    job_project_id: Optional[str] = None
    job_workspace_id: Optional[str] = None

    # Notebook settings
    notebook_resource: str = "1xH200"
    notebook_image: Optional[str] = None

    # SSH settings
    rtunnel_bin: Optional[str] = None
    sshd_deb_dir: Optional[str] = None
    dropbear_deb_dir: Optional[str] = None
    rtunnel_download_url: str = "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz"

    # Mirror settings
    apt_mirror_url: Optional[str] = None
    pip_index_url: Optional[str] = None
    pip_trusted_host: Optional[str] = None

    # Other
    default_shm: Optional[str] = None

    @classmethod
    def from_env(cls, require_target_dir: bool = False) -> "Config":
        """Create configuration from environment variables.

        Args:
            require_target_dir: If True, raise error if INSPIRE_TARGET_DIR is not set

        Returns:
            Config instance

        Raises:
            ConfigError: If required environment variables are missing
        """
        username = os.getenv("INSPIRE_USERNAME")
        password = os.getenv("INSPIRE_PASSWORD")

        if not username:
            raise ConfigError(
                "Missing INSPIRE_USERNAME environment variable.\n"
                "Set it with: export INSPIRE_USERNAME='your_username'"
            )

        if not password:
            raise ConfigError(
                "Missing INSPIRE_PASSWORD environment variable.\n"
                "Set it with: export INSPIRE_PASSWORD='your_password'"
            )

        target_dir = os.getenv("INSPIRE_TARGET_DIR")

        if require_target_dir and not target_dir:
            raise ConfigError(
                "Missing INSPIRE_TARGET_DIR environment variable.\n"
                "This is required for Bridge operations (sync, exec, logs).\n"
                "Set it with: export INSPIRE_TARGET_DIR='/path/to/shared/directory'"
            )

        # API tuning
        timeout = 30
        max_retries = 3
        retry_delay = 1.0

        timeout_env = os.getenv("INSPIRE_TIMEOUT")
        if timeout_env:
            try:
                timeout = int(timeout_env)
            except ValueError:
                raise ConfigError(
                    "Invalid INSPIRE_TIMEOUT value. It must be an integer number of seconds."
                )

        max_retries_env = os.getenv("INSPIRE_MAX_RETRIES")
        if max_retries_env:
            try:
                max_retries = int(max_retries_env)
            except ValueError:
                raise ConfigError(
                    "Invalid INSPIRE_MAX_RETRIES value. It must be an integer."
                )

        retry_delay_env = os.getenv("INSPIRE_RETRY_DELAY")
        if retry_delay_env:
            try:
                retry_delay = float(retry_delay_env)
            except ValueError:
                raise ConfigError(
                    "Invalid INSPIRE_RETRY_DELAY value. It must be a number of seconds."
                )

        bridge_action_timeout = 300
        bat_env = os.getenv("INSPIRE_BRIDGE_ACTION_TIMEOUT")
        if bat_env:
            try:
                bridge_action_timeout = int(bat_env)
            except ValueError:
                raise ConfigError(
                    "Invalid INSPIRE_BRIDGE_ACTION_TIMEOUT value. It must be an integer number of seconds."
                )

        return cls(
            username=username,
            password=password,
            base_url=os.getenv("INSPIRE_BASE_URL", "https://qz.sii.edu.cn"),
            target_dir=target_dir,
            log_pattern=os.getenv("INSPIRE_LOG_PATTERN", "training_master_*.log"),
            job_cache_path=os.getenv("INSPIRE_JOB_CACHE", "~/.inspire/jobs.json"),
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
            gitea_repo=os.getenv("INSP_GITEA_REPO"),
            gitea_token=os.getenv("INSP_GITEA_TOKEN"),
            gitea_server=os.getenv("INSP_GITEA_SERVER", "https://codeberg.org"),
            gitea_log_workflow=os.getenv("INSP_GITEA_LOG_WORKFLOW", "retrieve_job_log.yml"),
            gitea_sync_workflow=os.getenv("INSP_GITEA_SYNC_WORKFLOW", "sync_code.yml"),
            gitea_bridge_workflow=os.getenv("INSP_GITEA_BRIDGE_WORKFLOW", "run_bridge_action.yml"),
            log_cache_dir=os.getenv("INSP_LOG_CACHE_DIR", "~/.inspire/logs"),
            remote_timeout=_parse_remote_timeout(os.getenv("INSP_REMOTE_TIMEOUT", "90")),
            default_remote=os.getenv("INSPIRE_DEFAULT_REMOTE", "origin"),
            bridge_action_timeout=bridge_action_timeout,
            bridge_action_denylist=_parse_denylist(os.getenv("INSPIRE_BRIDGE_DENYLIST")),
        )

    @classmethod
    def from_env_for_sync(cls) -> "Config":
        """Create configuration for sync/bridge commands (doesn't require platform credentials).

        The sync and bridge exec commands only need Gitea access and target dir,
        not Inspire platform credentials.

        Returns:
            Config instance with sync-related settings

        Raises:
            ConfigError: If required environment variables are missing
        """
        # Check for target dir
        target_dir = os.getenv("INSPIRE_TARGET_DIR")
        if not target_dir:
            raise ConfigError(
                "Missing INSPIRE_TARGET_DIR environment variable.\n"
                "This specifies the target directory on the Bridge.\n"
                "Set it with: export INSPIRE_TARGET_DIR='/path/to/shared/directory'"
            )

        # Check for Gitea repo
        gitea_repo = os.getenv("INSP_GITEA_REPO")
        if not gitea_repo:
            raise ConfigError(
                "Missing INSP_GITEA_REPO environment variable.\n"
                "Set it with: export INSP_GITEA_REPO='owner/repo'"
            )

        gitea_server = os.getenv("INSP_GITEA_SERVER", "https://codeberg.org")

        bridge_action_timeout = 300
        bat_env = os.getenv("INSPIRE_BRIDGE_ACTION_TIMEOUT")
        if bat_env:
            try:
                bridge_action_timeout = int(bat_env)
            except ValueError:
                raise ConfigError(
                    "Invalid INSPIRE_BRIDGE_ACTION_TIMEOUT value. It must be an integer number of seconds."
                )

        return cls(
            # Use placeholder values for platform credentials since sync doesn't need them
            username="",
            password="",
            target_dir=target_dir,
            gitea_repo=gitea_repo,
            gitea_token=os.getenv("INSP_GITEA_TOKEN"),
            gitea_server=gitea_server,
            gitea_log_workflow=os.getenv("INSP_GITEA_LOG_WORKFLOW", "retrieve_job_log.yml"),
            gitea_sync_workflow=os.getenv("INSP_GITEA_SYNC_WORKFLOW", "sync_code.yml"),
            gitea_bridge_workflow=os.getenv("INSP_GITEA_BRIDGE_WORKFLOW", "run_bridge_action.yml"),
            default_remote=os.getenv("INSPIRE_DEFAULT_REMOTE", "origin"),
            remote_timeout=_parse_remote_timeout(os.getenv("INSP_REMOTE_TIMEOUT", "90")),
            bridge_action_timeout=bridge_action_timeout,
            bridge_action_denylist=_parse_denylist(os.getenv("INSPIRE_BRIDGE_DENYLIST")),
        )

    def get_expanded_cache_path(self) -> str:
        """Get the job cache path with ~ expanded."""
        return os.path.expanduser(self.job_cache_path)

    # Class-level config paths
    GLOBAL_CONFIG_PATH = Path.home() / ".config" / "inspire" / CONFIG_FILENAME

    @classmethod
    def _find_project_config(cls) -> Path | None:
        """Walk up from cwd to find inspire/config.toml."""
        current = Path.cwd()
        while current != current.parent:
            config_path = current / PROJECT_CONFIG_DIR / CONFIG_FILENAME
            if config_path.exists():
                return config_path
            current = current.parent
        return None

    @staticmethod
    def _load_toml(path: Path) -> dict[str, Any]:
        """Load and parse a TOML config file."""
        with open(path, "rb") as f:
            return tomllib.load(f)

    @staticmethod
    def _flatten_toml(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        """Flatten nested TOML dict to dotted keys (e.g., auth.username)."""
        result = {}
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(Config._flatten_toml(value, full_key))
            else:
                result[full_key] = value
        return result

    @classmethod
    def _toml_key_to_field(cls, toml_key: str) -> str | None:
        """Map TOML key to Config field name."""
        mapping = {
            "auth.username": "username",
            "auth.password": "password",
            "api.base_url": "base_url",
            "api.timeout": "timeout",
            "api.max_retries": "max_retries",
            "api.retry_delay": "retry_delay",
            "api.skip_ssl_verify": "skip_ssl_verify",
            "api.force_proxy": "force_proxy",
            "api.openapi_prefix": "openapi_prefix",
            "api.browser_api_prefix": "browser_api_prefix",
            "api.auth_endpoint": "auth_endpoint",
            "api.docker_registry": "docker_registry",
            "paths.target_dir": "target_dir",
            "paths.log_pattern": "log_pattern",
            "paths.job_cache": "job_cache_path",
            "paths.log_cache_dir": "log_cache_dir",
            "gitea.server": "gitea_server",
            "gitea.repo": "gitea_repo",
            "gitea.token": "gitea_token",
            "gitea.log_workflow": "gitea_log_workflow",
            "gitea.sync_workflow": "gitea_sync_workflow",
            "gitea.bridge_workflow": "gitea_bridge_workflow",
            "gitea.remote_timeout": "remote_timeout",
            "sync.default_remote": "default_remote",
            "bridge.action_timeout": "bridge_action_timeout",
            "bridge.denylist": "bridge_action_denylist",
            "job.priority": "job_priority",
            "job.image": "job_image",
            "job.project_id": "job_project_id",
            "job.workspace_id": "job_workspace_id",
            "notebook.resource": "notebook_resource",
            "notebook.image": "notebook_image",
            "ssh.rtunnel_bin": "rtunnel_bin",
            "ssh.sshd_deb_dir": "sshd_deb_dir",
            "ssh.dropbear_deb_dir": "dropbear_deb_dir",
            "ssh.rtunnel_download_url": "rtunnel_download_url",
            "mirrors.apt_mirror_url": "apt_mirror_url",
            "mirrors.pip_index_url": "pip_index_url",
            "mirrors.pip_trusted_host": "pip_trusted_host",
            "other.default_shm": "default_shm",
        }
        return mapping.get(toml_key)

    @classmethod
    def from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ) -> tuple["Config", dict[str, str]]:
        """Load config from files + env vars with layered precedence.

        Precedence (lowest to highest):
            Hardcoded defaults < Global config.toml < Project config.toml < Environment variables

        Args:
            require_target_dir: If True, raise error if target_dir is not set
            require_credentials: If True, raise error if username/password not set

        Returns:
            Tuple of (Config instance, dict mapping field names to their sources)

        Raises:
            ConfigError: If required configuration is missing
        """
        # Track where each value came from
        sources: dict[str, str] = {}

        # 1. Start with defaults
        config_dict: dict[str, Any] = {
            "username": "",
            "password": "",
            "base_url": "https://qz.sii.edu.cn",
            "target_dir": None,
            "log_pattern": "training_master_*.log",
            "job_cache_path": "~/.inspire/jobs.json",
            "timeout": 30,
            "max_retries": 3,
            "retry_delay": 1.0,
            "gitea_repo": None,
            "gitea_token": None,
            "gitea_server": "https://codeberg.org",
            "gitea_log_workflow": "retrieve_job_log.yml",
            "gitea_sync_workflow": "sync_code.yml",
            "gitea_bridge_workflow": "run_bridge_action.yml",
            "log_cache_dir": "~/.inspire/logs",
            "remote_timeout": 90,
            "default_remote": "origin",
            "bridge_action_timeout": 300,
            "bridge_action_denylist": [],
            # API settings (additional)
            "skip_ssl_verify": False,
            "force_proxy": False,
            # API path prefixes
            "openapi_prefix": None,
            "browser_api_prefix": None,
            "auth_endpoint": None,
            "docker_registry": None,
            # Job settings
            "job_priority": 6,
            "job_image": None,
            "job_project_id": None,
            "job_workspace_id": None,
            # Notebook settings
            "notebook_resource": "1xH200",
            "notebook_image": None,
            # SSH settings
            "rtunnel_bin": None,
            "sshd_deb_dir": None,
            "dropbear_deb_dir": None,
            "rtunnel_download_url": "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz",
            # Mirror settings
            "apt_mirror_url": None,
            "pip_index_url": None,
            "pip_trusted_host": None,
            # Other
            "default_shm": None,
        }

        # Mark all as defaults initially
        for key in config_dict:
            sources[key] = SOURCE_DEFAULT

        # 2. Merge global config
        global_config_path: Path | None = None
        if cls.GLOBAL_CONFIG_PATH.exists():
            global_config_path = cls.GLOBAL_CONFIG_PATH
            flat_global = cls._flatten_toml(cls._load_toml(cls.GLOBAL_CONFIG_PATH))
            for toml_key, value in flat_global.items():
                field_name = cls._toml_key_to_field(toml_key)
                if field_name and field_name in config_dict:
                    config_dict[field_name] = value
                    sources[field_name] = SOURCE_GLOBAL

        # 3. Merge project config (walk up from cwd to find inspire/config.toml)
        project_config_path = cls._find_project_config()
        if project_config_path:
            flat_project = cls._flatten_toml(cls._load_toml(project_config_path))
            for toml_key, value in flat_project.items():
                field_name = cls._toml_key_to_field(toml_key)
                if field_name and field_name in config_dict:
                    config_dict[field_name] = value
                    sources[field_name] = SOURCE_PROJECT

        # 4. Override with env vars (highest priority)
        env_mapping = {
            "INSPIRE_USERNAME": "username",
            "INSPIRE_PASSWORD": "password",
            "INSPIRE_BASE_URL": "base_url",
            "INSPIRE_TARGET_DIR": "target_dir",
            "INSPIRE_LOG_PATTERN": "log_pattern",
            "INSPIRE_JOB_CACHE": "job_cache_path",
            "INSPIRE_TIMEOUT": ("timeout", int),
            "INSPIRE_MAX_RETRIES": ("max_retries", int),
            "INSPIRE_RETRY_DELAY": ("retry_delay", float),
            "INSP_GITEA_REPO": "gitea_repo",
            "INSP_GITEA_TOKEN": "gitea_token",
            "INSP_GITEA_SERVER": "gitea_server",
            "INSP_GITEA_LOG_WORKFLOW": "gitea_log_workflow",
            "INSP_GITEA_SYNC_WORKFLOW": "gitea_sync_workflow",
            "INSP_GITEA_BRIDGE_WORKFLOW": "gitea_bridge_workflow",
            "INSP_LOG_CACHE_DIR": "log_cache_dir",
            "INSP_REMOTE_TIMEOUT": ("remote_timeout", int),
            "INSPIRE_DEFAULT_REMOTE": "default_remote",
            "INSPIRE_BRIDGE_ACTION_TIMEOUT": ("bridge_action_timeout", int),
            "INSPIRE_BRIDGE_DENYLIST": ("bridge_action_denylist", _parse_denylist),
            # API settings (additional)
            "INSPIRE_SKIP_SSL_VERIFY": ("skip_ssl_verify", _parse_bool),
            "INSPIRE_FORCE_PROXY": ("force_proxy", _parse_bool),
            # API path prefixes
            "INSPIRE_OPENAPI_PREFIX": "openapi_prefix",
            "INSPIRE_BROWSER_API_PREFIX": "browser_api_prefix",
            "INSPIRE_AUTH_ENDPOINT": "auth_endpoint",
            "INSPIRE_DOCKER_REGISTRY": "docker_registry",
            # Job settings
            "INSP_PRIORITY": ("job_priority", int),
            "INSP_IMAGE": "job_image",
            "INSPIRE_PROJECT_ID": "job_project_id",
            "INSPIRE_WORKSPACE_ID": "job_workspace_id",
            # Notebook settings
            "INSPIRE_NOTEBOOK_RESOURCE": "notebook_resource",
            "INSPIRE_NOTEBOOK_IMAGE": "notebook_image",
            # SSH settings
            "INSPIRE_RTUNNEL_BIN": "rtunnel_bin",
            "INSPIRE_SSHD_DEB_DIR": "sshd_deb_dir",
            "INSPIRE_DROPBEAR_DEB_DIR": "dropbear_deb_dir",
            "INSPIRE_RTUNNEL_DOWNLOAD_URL": "rtunnel_download_url",
            # Mirror settings
            "INSPIRE_APT_MIRROR_URL": "apt_mirror_url",
            "INSPIRE_PIP_INDEX_URL": "pip_index_url",
            "INSPIRE_PIP_TRUSTED_HOST": "pip_trusted_host",
            # Other
            "DEFAULT_SHM_ENV_VAR": "default_shm",
        }

        for env_var, mapping in env_mapping.items():
            value = os.getenv(env_var)
            if value is not None:
                if isinstance(mapping, tuple):
                    field_name, parser = mapping
                    try:
                        if parser == _parse_denylist:
                            parsed_value = parser(value)
                        else:
                            parsed_value = parser(value)
                    except (ValueError, TypeError):
                        raise ConfigError(f"Invalid {env_var} value: {value}")
                    config_dict[field_name] = parsed_value
                else:
                    field_name = mapping
                    config_dict[field_name] = value
                sources[field_name] = SOURCE_ENV

        # Validate required fields
        if require_credentials:
            if not config_dict["username"]:
                raise ConfigError(
                    "Missing username configuration.\n"
                    "Set INSPIRE_USERNAME env var or add to config.toml:\n"
                    "  [auth]\n"
                    "  username = 'your_username'"
                )
            if not config_dict["password"]:
                raise ConfigError(
                    "Missing password configuration.\n"
                    "Set INSPIRE_PASSWORD env var (recommended for security)"
                )

        if require_target_dir and not config_dict["target_dir"]:
            raise ConfigError(
                "Missing target directory configuration.\n"
                "Set INSPIRE_TARGET_DIR env var or add to config.toml:\n"
                "  [paths]\n"
                "  target_dir = '/path/to/shared/directory'"
            )

        # Store config file paths for reference
        config_dict["_global_config_path"] = global_config_path
        config_dict["_project_config_path"] = project_config_path

        # Remove internal keys before creating Config
        global_path = config_dict.pop("_global_config_path", None)
        project_path = config_dict.pop("_project_config_path", None)

        config = cls(**config_dict)

        # Attach paths for display purposes
        config._global_config_path = global_path  # type: ignore
        config._project_config_path = project_path  # type: ignore
        config._sources = sources  # type: ignore

        return config, sources

    @classmethod
    def get_config_paths(cls) -> tuple[Path | None, Path | None]:
        """Get paths to global and project config files if they exist.

        Returns:
            Tuple of (global_config_path, project_config_path) - None if not found
        """
        global_path = cls.GLOBAL_CONFIG_PATH if cls.GLOBAL_CONFIG_PATH.exists() else None
        project_path = cls._find_project_config()
        return global_path, project_path
