"""Configuration management for Inspire CLI.

Reads configuration from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


class ConfigError(Exception):
    """Configuration error - missing or invalid settings."""

    pass


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

    **GitHub bridge (required for sync, bridge exec, remote logs):**
    - INSP_GITHUB_REPO: GitHub repo as 'owner/repo'
    - INSP_GITHUB_TOKEN: GitHub Personal Access Token (or use `gh auth token`)
    - INSP_GITHUB_WORKFLOW: Log retrieval workflow filename (default: retrieve_job_log.yml)
    - INSP_LOG_CACHE_DIR: Cache directory for remote logs (default: ~/.inspire/logs)
    - INSP_REMOTE_TIMEOUT: Max time to wait for artifact (seconds, default: 90)

    **Job cache (optional):**
    - INSPIRE_JOB_CACHE: Local job cache location (default: ~/.inspire/jobs.json)

    **API tuning (optional):**
    - INSPIRE_TIMEOUT: API timeout in seconds (default: 30)
    - INSPIRE_MAX_RETRIES: Max API retries (default: 3)
    - INSPIRE_RETRY_DELAY: Retry delay in seconds (default: 1.0)

    **Bridge exec settings:**
    - INSPIRE_BRIDGE_ACTION_WORKFLOW: Workflow filename (default: run_bridge_action.yml)
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

    # GitHub / remote log settings
    github_repo: Optional[str] = None
    github_workflow: str = "retrieve_job_log.yml"
    github_token: Optional[str] = None
    log_cache_dir: str = "~/.inspire/logs"
    remote_timeout: int = 90

    # Sync settings
    default_remote: str = "origin"
    sync_workflow: str = "sync_code.yml"

    # Bridge action settings
    bridge_action_workflow: str = "run_bridge_action.yml"
    bridge_action_timeout: int = 300
    bridge_action_denylist: list[str] = field(default_factory=list)

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
            github_repo=os.getenv("INSP_GITHUB_REPO"),
            github_workflow=os.getenv("INSP_GITHUB_WORKFLOW", "retrieve_job_log.yml"),
            github_token=os.getenv("INSP_GITHUB_TOKEN"),
            log_cache_dir=os.getenv("INSP_LOG_CACHE_DIR", "~/.inspire/logs"),
            remote_timeout=_parse_remote_timeout(os.getenv("INSP_REMOTE_TIMEOUT", "90")),
            default_remote=os.getenv("INSPIRE_DEFAULT_REMOTE", "origin"),
            sync_workflow=os.getenv("INSPIRE_SYNC_WORKFLOW", "sync_code.yml"),
            bridge_action_workflow=os.getenv("INSPIRE_BRIDGE_ACTION_WORKFLOW", "run_bridge_action.yml"),
            bridge_action_timeout=bridge_action_timeout,
            bridge_action_denylist=_parse_denylist(os.getenv("INSPIRE_BRIDGE_DENYLIST")),
        )

    @classmethod
    def from_env_for_sync(cls) -> "Config":
        """Create configuration for sync/bridge commands (doesn't require platform credentials).

        The sync and bridge exec commands only need GitHub access and target dir,
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

        # Check for GitHub repo
        github_repo = os.getenv("INSP_GITHUB_REPO")
        if not github_repo:
            raise ConfigError(
                "Missing INSP_GITHUB_REPO environment variable.\n"
                "Set it with: export INSP_GITHUB_REPO='owner/repo'"
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
            # Use placeholder values for platform credentials since sync doesn't need them
            username="",
            password="",
            target_dir=target_dir,
            github_repo=github_repo,
            github_token=os.getenv("INSP_GITHUB_TOKEN"),
            default_remote=os.getenv("INSPIRE_DEFAULT_REMOTE", "origin"),
            sync_workflow=os.getenv("INSPIRE_SYNC_WORKFLOW", "sync_code.yml"),
            remote_timeout=_parse_remote_timeout(os.getenv("INSP_REMOTE_TIMEOUT", "90")),
            bridge_action_workflow=os.getenv("INSPIRE_BRIDGE_ACTION_WORKFLOW", "run_bridge_action.yml"),
            bridge_action_timeout=bridge_action_timeout,
            bridge_action_denylist=_parse_denylist(os.getenv("INSPIRE_BRIDGE_DENYLIST")),
        )

    def get_expanded_cache_path(self) -> str:
        """Get the job cache path with ~ expanded."""
        return os.path.expanduser(self.job_cache_path)
