"""Config file loading + merging for Inspire CLI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from inspire.config.models import (
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    Config,
    ConfigError,
)
from inspire.config.schema import CONFIG_OPTIONS
from inspire.config.toml import (
    _find_project_config,
    _flatten_toml,
    _load_toml,
    _toml_key_to_field,
)


def _parse_global_accounts(raw_accounts: Any) -> dict[str, str]:
    """Parse global [accounts.\"<username>\"] password entries."""
    if not isinstance(raw_accounts, dict):
        return {}

    parsed: dict[str, str] = {}
    for raw_username, raw_value in raw_accounts.items():
        username = str(raw_username).strip()
        if not username or not isinstance(raw_value, dict):
            continue
        password = raw_value.get("password")
        if password is None:
            continue
        password_str = str(password)
        if not password_str:
            continue
        parsed[username] = password_str
    return parsed


def config_from_files_and_env(
    *,
    require_target_dir: bool = False,
    require_credentials: bool = True,
) -> tuple[Config, dict[str, str]]:
    """Load config from files + env vars with layered precedence."""
    sources: dict[str, str] = {}

    config_dict: dict[str, Any] = {
        "username": "",
        "password": "",
        "base_url": "https://api.example.com",
        "target_dir": None,
        "log_pattern": "training_master_*.log",
        "job_cache_path": "~/.inspire/jobs.json",
        "timeout": 30,
        "max_retries": 3,
        "retry_delay": 1.0,
        "git_platform": None,
        "gitea_repo": None,
        "gitea_token": None,
        "gitea_server": "https://codeberg.org",
        "gitea_log_workflow": "retrieve_job_log.yml",
        "gitea_sync_workflow": "sync_code.yml",
        "gitea_bridge_workflow": "run_bridge_action.yml",
        "github_repo": None,
        "github_token": None,
        "github_server": "https://github.com",
        "github_log_workflow": "retrieve_job_log.yml",
        "github_sync_workflow": "sync_code.yml",
        "github_bridge_workflow": "run_bridge_action.yml",
        "log_cache_dir": "~/.inspire/logs",
        "remote_timeout": 90,
        "default_remote": "origin",
        "bridge_action_timeout": 600,
        "bridge_action_denylist": [],
        "skip_ssl_verify": False,
        "force_proxy": False,
        "openapi_prefix": None,
        "browser_api_prefix": None,
        "auth_endpoint": None,
        "docker_registry": None,
        "job_priority": 6,
        "job_image": None,
        "job_project_id": None,
        "job_workspace_id": None,
        "workspace_cpu_id": None,
        "workspace_gpu_id": None,
        "workspace_internet_id": None,
        "workspaces": {},
        "notebook_resource": "1xH200",
        "notebook_image": None,
        "rtunnel_bin": None,
        "sshd_deb_dir": None,
        "dropbear_deb_dir": None,
        "setup_script": None,
        "rtunnel_download_url": (
            "https://github.com/Sarfflow/rtunnel/releases/download/nightly/"
            "rtunnel-linux-amd64.tar.gz"
        ),
        "apt_mirror_url": None,
        "pip_index_url": None,
        "pip_trusted_host": None,
        "tunnel_retries": 3,
        "tunnel_retry_pause": 2.0,
        "shm_size": None,
        "compute_groups": [],
        "remote_env": {},
        "accounts": {},
    }

    for key in config_dict:
        sources[key] = SOURCE_DEFAULT

    global_config_path: Path | None = None
    global_compute_groups: list[dict] = []
    global_remote_env: dict[str, str] = {}
    global_workspaces: dict[str, str] = {}
    global_accounts: dict[str, str] = {}
    if Config.GLOBAL_CONFIG_PATH.exists():
        global_config_path = Config.GLOBAL_CONFIG_PATH
        global_raw = _load_toml(Config.GLOBAL_CONFIG_PATH)
        global_compute_groups = global_raw.pop("compute_groups", [])
        global_remote_env = {str(k): str(v) for k, v in global_raw.pop("remote_env", {}).items()}
        global_accounts = _parse_global_accounts(global_raw.pop("accounts", {}))

        raw_workspaces = global_raw.get("workspaces") or {}
        if isinstance(raw_workspaces, dict):
            global_workspaces = {str(k): str(v) for k, v in raw_workspaces.items()}
        flat_global = _flatten_toml(global_raw)
        for toml_key, value in flat_global.items():
            field_name = _toml_key_to_field(toml_key)
            if field_name and field_name in config_dict:
                config_dict[field_name] = value
                sources[field_name] = SOURCE_GLOBAL
        if global_compute_groups:
            config_dict["compute_groups"] = global_compute_groups
            sources["compute_groups"] = SOURCE_GLOBAL
        if global_remote_env:
            config_dict["remote_env"] = global_remote_env
            sources["remote_env"] = SOURCE_GLOBAL
        if global_workspaces:
            config_dict["workspaces"] = global_workspaces
            sources["workspaces"] = SOURCE_GLOBAL
        if global_accounts:
            config_dict["accounts"] = global_accounts
            sources["accounts"] = SOURCE_GLOBAL

    project_config_path = _find_project_config()
    project_compute_groups: list[dict] = []
    project_remote_env: dict[str, str] = {}
    project_workspaces: dict[str, str] = {}
    prefer_source = "env"
    if project_config_path:
        project_raw = _load_toml(project_config_path)

        # Extract cli.prefer_source before flattening
        cli_section = project_raw.pop("cli", {})
        prefer_source = cli_section.get("prefer_source", "env")
        if prefer_source not in ("env", "toml"):
            raise ConfigError(
                f"Invalid prefer_source value: '{prefer_source}'\n"
                "Must be 'env' or 'toml' in [cli] section of project config."
            )

        project_compute_groups = project_raw.pop("compute_groups", [])
        project_remote_env = {str(k): str(v) for k, v in project_raw.pop("remote_env", {}).items()}
        # Keep account passwords global-only to avoid storing secrets in repos.
        project_raw.pop("accounts", None)

        raw_workspaces = project_raw.get("workspaces") or {}
        if isinstance(raw_workspaces, dict):
            project_workspaces = {str(k): str(v) for k, v in raw_workspaces.items()}
        flat_project = _flatten_toml(project_raw)
        for toml_key, value in flat_project.items():
            field_name = _toml_key_to_field(toml_key)
            if field_name and field_name in config_dict:
                config_dict[field_name] = value
                sources[field_name] = SOURCE_PROJECT
        if project_compute_groups:
            config_dict["compute_groups"] = project_compute_groups
            sources["compute_groups"] = SOURCE_PROJECT
        if project_remote_env:
            merged_remote_env = dict(config_dict.get("remote_env", {}))
            merged_remote_env.update(project_remote_env)
            config_dict["remote_env"] = merged_remote_env
            sources["remote_env"] = SOURCE_PROJECT
        if project_workspaces:
            merged_workspaces = dict(config_dict.get("workspaces", {}))
            merged_workspaces.update(project_workspaces)
            config_dict["workspaces"] = merged_workspaces
            sources["workspaces"] = SOURCE_PROJECT

    env_password = os.getenv("INSPIRE_PASSWORD")

    for option in CONFIG_OPTIONS:
        if option.env_var == "INSPIRE_PASSWORD":
            continue

        value = os.getenv(option.env_var)
        if value is None and option.env_var == "INSP_LOG_CACHE_DIR":
            value = os.getenv("INSPIRE_LOG_CACHE_DIR")
        if value is None:
            continue

        field_name = option.field_name
        if field_name not in config_dict:
            continue

        if option.parser:
            try:
                parsed_value = option.parser(value)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"Invalid {option.env_var} value: {value}") from e
            new_value = parsed_value
        else:
            new_value = value

        # If prefer_source is "toml", skip env override for project-sourced fields
        if prefer_source == "toml" and sources.get(field_name) == SOURCE_PROJECT:
            continue

        config_dict[field_name] = new_value
        sources[field_name] = SOURCE_ENV

    # Password precedence:
    # 1) explicit [auth].password from config layers
    # 2) global [accounts."<username>"].password
    # 3) INSPIRE_PASSWORD env var (fallback only if still unset)
    if not config_dict.get("password"):
        resolved_username = str(config_dict.get("username") or "").strip()
        account_password = config_dict.get("accounts", {}).get(resolved_username)
        if account_password:
            config_dict["password"] = account_password
            sources["password"] = SOURCE_GLOBAL

    if not config_dict.get("password") and env_password:
        config_dict["password"] = env_password
        sources["password"] = SOURCE_ENV

    if not config_dict.get("github_token"):
        github_token_fallback = os.getenv("GITHUB_TOKEN")
        if github_token_fallback:
            config_dict["github_token"] = github_token_fallback
            sources["github_token"] = SOURCE_ENV

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
                "Set INSPIRE_PASSWORD env var or add a global account password:\n"
                "  [accounts.\"your_username\"]\n"
                "  password = 'your_password'"
            )

    if require_target_dir and not config_dict["target_dir"]:
        raise ConfigError(
            "Missing target directory configuration.\n"
            "Set INSPIRE_TARGET_DIR env var or add to config.toml:\n"
            "  [paths]\n"
            "  target_dir = '/path/to/shared/directory'"
        )

    config_dict["_global_config_path"] = global_config_path
    config_dict["_project_config_path"] = project_config_path
    config_dict["prefer_source"] = prefer_source

    global_path = config_dict.pop("_global_config_path", None)
    project_path = config_dict.pop("_project_config_path", None)

    config = Config(**config_dict)

    config._global_config_path = global_path  # type: ignore[attr-defined]
    config._project_config_path = project_path  # type: ignore[attr-defined]
    config._sources = sources  # type: ignore[attr-defined]

    return config, sources


def get_config_paths() -> tuple[Path | None, Path | None]:
    global_path = Config.GLOBAL_CONFIG_PATH if Config.GLOBAL_CONFIG_PATH.exists() else None
    project_path = _find_project_config()
    return global_path, project_path
