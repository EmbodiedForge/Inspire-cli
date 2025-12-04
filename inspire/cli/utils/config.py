"""Configuration management for Inspire CLI.

Reads configuration from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass
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


@dataclass
class Config:
    """Inspire CLI configuration.

    All configuration is read from environment variables:

    **Platform API (required):**
    - INSPIRE_USERNAME: Platform username
    - INSPIRE_PASSWORD: Platform password
    - INSPIRE_BASE_URL: API base URL (default: https://qz.sii.edu.cn)

    **Job creation and log retrieval:**
    - INSPIRE_TARGET_LOG_DIR: Shared filesystem path on Bridge where logs are written (e.g., /train/logs)
      - REQUIRED for both job creation and log retrieval
      - Gets embedded in the wrapped command sent to Inspire API during job creation
      - Used to locate logs on Bridge during retrieval
      - Example: command gets wrapped as: (cd /training/code && bash train.sh) > /train/logs/job_name.log 2>&1
    - INSPIRE_LOG_PATTERN: Log file glob pattern (default: training_master_*.log)

    **Log retrieval (remote mode, GitHub bridge):**
    - INSPIRE_TARGET_LOG_DIR: Same path as used during job creation (e.g., /train/logs)
    - INSP_GITHUB_REPO: GitHub repo as 'owner/repo' (required for remote log fetch)
    - INSP_GITHUB_WORKFLOW: Workflow filename (default: retrieve_job_log.yml)
    - INSP_GITHUB_TOKEN: GitHub Personal Access Token (or use `gh auth token`)
    - INSP_LOG_CACHE_DIR: Cache directory for remote logs (default: ~/.inspire/logs)
    - INSP_REMOTE_TIMEOUT: Max time to wait for artifact (seconds, default: 90)
      - First fetch: ~20-30 seconds; cached fetches: instant.

    **Job cache (optional):**
    - INSPIRE_JOB_CACHE: Local job cache location (default: ~/.inspire/jobs.json)

    **API tuning (optional):**
    - INSPIRE_TIMEOUT: API timeout in seconds (default: 30)
    - INSPIRE_MAX_RETRIES: Max API retries (default: 3)
    - INSPIRE_RETRY_DELAY: Retry delay in seconds (default: 1.0)
    """

    # Required
    username: str
    password: str

    # Optional with defaults
    base_url: str = "https://qz.sii.edu.cn"
    target_dir: Optional[str] = None  # INSPIRE_TARGET_LOG_DIR - used for both job creation and log retrieval
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
    sync_target_dir: Optional[str] = None

    @classmethod
    def from_env(cls, require_target_dir: bool = False) -> "Config":
        """Create configuration from environment variables.

        Args:
            require_target_dir: If True, raise error if INSP_TARGET_DIR is not set

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

        target_dir = os.getenv("INSPIRE_TARGET_LOG_DIR")

        if require_target_dir and not target_dir:
            raise ConfigError(
                "Missing INSPIRE_TARGET_LOG_DIR environment variable.\n"
                "This is required for both job creation and log retrieval.\n"
                "Set it with: export INSPIRE_TARGET_LOG_DIR='/path/to/shared/logs/directory'"
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
            sync_target_dir=os.getenv("INSPIRE_SYNC_TARGET_DIR"),
        )

    @classmethod
    def from_env_for_sync(cls) -> "Config":
        """Create configuration for sync command (doesn't require platform credentials).

        The sync command only needs GitHub access and sync settings, not Inspire
        platform credentials.

        Returns:
            Config instance with sync-related settings

        Raises:
            ConfigError: If required environment variables are missing
        """
        # Check for sync target dir
        sync_target_dir = os.getenv("INSPIRE_SYNC_TARGET_DIR")
        if not sync_target_dir:
            raise ConfigError(
                "Missing INSPIRE_SYNC_TARGET_DIR environment variable.\n"
                "This specifies the target directory on the Bridge where code will be synced.\n"
                "Set it with: export INSPIRE_SYNC_TARGET_DIR='/path/to/shared/code/directory'"
            )

        # Check for GitHub repo
        github_repo = os.getenv("INSP_GITHUB_REPO")
        if not github_repo:
            raise ConfigError(
                "Missing INSP_GITHUB_REPO environment variable.\n"
                "Set it with: export INSP_GITHUB_REPO='owner/repo'"
            )

        return cls(
            # Use placeholder values for platform credentials since sync doesn't need them
            username="",
            password="",
            github_repo=github_repo,
            github_token=os.getenv("INSP_GITHUB_TOKEN"),
            default_remote=os.getenv("INSPIRE_DEFAULT_REMOTE", "origin"),
            sync_workflow=os.getenv("INSPIRE_SYNC_WORKFLOW", "sync_code.yml"),
            sync_target_dir=sync_target_dir,
            remote_timeout=_parse_remote_timeout(os.getenv("INSP_REMOTE_TIMEOUT", "90")),
        )

    def get_expanded_cache_path(self) -> str:
        """Get the job cache path with ~ expanded."""
        return os.path.expanduser(self.job_cache_path)
