"""Config file loading + merging for Inspire CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass
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

_ACCOUNT_OVERRIDE_FIELDS = {
    "base_url",
    "timeout",
    "max_retries",
    "retry_delay",
    "skip_ssl_verify",
    "force_proxy",
    "openapi_prefix",
    "browser_api_prefix",
    "auth_endpoint",
    "docker_registry",
    "rtunnel_bin",
    "sshd_deb_dir",
    "dropbear_deb_dir",
    "setup_script",
    "rtunnel_download_url",
}

_ACCOUNT_SECTION_KEY_MAP = {
    "api": {
        "base_url": "base_url",
        "timeout": "timeout",
        "max_retries": "max_retries",
        "retry_delay": "retry_delay",
        "skip_ssl_verify": "skip_ssl_verify",
        "force_proxy": "force_proxy",
        "openapi_prefix": "openapi_prefix",
        "browser_api_prefix": "browser_api_prefix",
        "auth_endpoint": "auth_endpoint",
        "docker_registry": "docker_registry",
    },
    "ssh": {
        "rtunnel_bin": "rtunnel_bin",
        "sshd_deb_dir": "sshd_deb_dir",
        "dropbear_deb_dir": "dropbear_deb_dir",
        "setup_script": "setup_script",
        "rtunnel_download_url": "rtunnel_download_url",
    },
}

_DEFAULTS_FIELD_MAP = {
    "image": "job_image",
    "notebook_image": "notebook_image",
    "notebook_resource": "notebook_resource",
    "priority": "job_priority",
    "shm_size": "shm_size",
    "target_dir": "target_dir",
    "log_pattern": "log_pattern",
    "project_order": "project_order",
}

_CONTEXT_WORKSPACE_FIELD_MAP = {
    "workspace": "job_workspace_id",
    "workspace_cpu": "workspace_cpu_id",
    "workspace_gpu": "workspace_gpu_id",
    "workspace_internet": "workspace_internet_id",
}


@dataclass
class _ProjectLayerState:
    project_config_path: Path | None
    project_projects: dict[str, str]
    project_defaults: dict[str, Any]
    project_context: dict[str, Any]
    project_account_catalogs: dict[str, dict[str, Any]]
    project_accounts: dict[str, str]
    prefer_source: str = "env"


def _default_config_values() -> dict[str, Any]:
    return {
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
        "projects": {},
        "project_catalog": {},
        "project_shared_path_groups": {},
        "project_workdirs": {},
        "account_shared_path_group": None,
        "account_train_job_workdir": None,
        "context_account": None,
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
        "tunnel_retries": 3,
        "tunnel_retry_pause": 2.0,
        "shm_size": None,
        "compute_groups": [],
        "remote_env": {},
        "accounts": {},
    }


def _initialize_sources(config_dict: dict[str, Any]) -> dict[str, str]:
    return {key: SOURCE_DEFAULT for key in config_dict}


def _apply_defaults_overrides(
    *,
    defaults: dict[str, Any],
    config_dict: dict[str, Any],
    sources: dict[str, str],
    source_name: str,
) -> None:
    for key, field_name in _DEFAULTS_FIELD_MAP.items():
        if key not in defaults:
            continue
        raw_value = defaults.get(key)
        if raw_value is None or raw_value == "":
            continue
        try:
            coerced = _coerce_project_default(field_name, raw_value)
        except (ValueError, TypeError):
            continue
        config_dict[field_name] = coerced
        sources[field_name] = source_name


def _parse_alias_map(raw_value: Any) -> dict[str, str]:
    if not isinstance(raw_value, dict):
        return {}

    result: dict[str, str] = {}
    for raw_key, raw_item in raw_value.items():
        key = str(raw_key).strip()
        value = str(raw_item).strip()
        if not key or not value:
            continue
        result[key] = value
    return result


def _normalize_compute_groups(raw_value: Any) -> list[dict]:
    if not isinstance(raw_value, list):
        return []

    normalized: list[dict] = []
    for raw_item in raw_value:
        if not isinstance(raw_item, dict):
            continue

        raw_ws = raw_item.get("workspace_ids", [])
        if isinstance(raw_ws, str):
            workspace_ids = [raw_ws] if raw_ws else []
        elif isinstance(raw_ws, list):
            workspace_ids = [str(w) for w in raw_ws if isinstance(w, str) and w]
        else:
            workspace_ids = []

        entry: dict = {
            "name": str(raw_item.get("name", "")),
            "id": str(raw_item.get("id", "")),
            "gpu_type": str(raw_item.get("gpu_type", "")),
            "location": str(raw_item.get("location", "")),
        }
        if workspace_ids:
            entry["workspace_ids"] = workspace_ids
        normalized.append(entry)
    return normalized


def _normalize_project_catalog(raw_value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_value, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for raw_project_id, raw_item in raw_value.items():
        project_id = str(raw_project_id).strip()
        if not project_id or not isinstance(raw_item, dict):
            continue
        entry: dict[str, Any] = {}
        for raw_key, value in raw_item.items():
            key = str(raw_key).strip()
            if not key or value is None:
                continue
            entry[key] = value
        normalized[project_id] = entry
    return normalized


def _resolve_alias(value: Any, mapping: dict[str, str], *, id_prefix: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    # Prefer explicit alias mappings (even if the alias looks like an ID prefix).
    if text in mapping:
        return mapping[text]
    for key, mapped in mapping.items():
        if key.lower() == text.lower():
            return mapped
    if text.startswith(id_prefix):
        return text
    return text


def _coerce_project_default(field_name: str, raw_value: Any) -> Any:
    if field_name in {"job_priority", "shm_size"}:
        return int(raw_value)
    if field_name in {
        "target_dir",
        "job_image",
        "notebook_image",
        "notebook_resource",
        "log_pattern",
    }:
        return str(raw_value)
    if field_name == "project_order":
        if isinstance(raw_value, list):
            return [str(v) for v in raw_value]
        return raw_value
    return raw_value


def _parse_global_accounts(raw_accounts: Any) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Parse [accounts.\"<username>\"] entries from a TOML layer."""
    if not isinstance(raw_accounts, dict):
        return {}, {}

    passwords: dict[str, str] = {}
    catalogs: dict[str, dict[str, Any]] = {}
    for raw_username, raw_value in raw_accounts.items():
        username = str(raw_username).strip()
        if not username or not isinstance(raw_value, dict):
            continue

        account_data: dict[str, Any] = {
            "projects": _parse_alias_map(raw_value.get("projects", {})),
            "workspaces": _parse_alias_map(raw_value.get("workspaces", {})),
            "compute_groups": _normalize_compute_groups(raw_value.get("compute_groups", [])),
            "project_catalog": _normalize_project_catalog(raw_value.get("project_catalog", {})),
            "shared_path_group": str(raw_value.get("shared_path_group") or "").strip() or None,
            "train_job_workdir": str(raw_value.get("train_job_workdir") or "").strip() or None,
            "overrides": {},
        }

        password = raw_value.get("password")
        if password is not None:
            password_str = str(password)
            if password_str:
                passwords[username] = password_str

        for field_name in _ACCOUNT_OVERRIDE_FIELDS:
            value = raw_value.get(field_name)
            if value is None or value == "":
                continue
            account_data["overrides"][field_name] = value

        for section_name, key_map in _ACCOUNT_SECTION_KEY_MAP.items():
            section = raw_value.get(section_name)
            if not isinstance(section, dict):
                continue
            for key, field_name in key_map.items():
                value = section.get(key)
                if value is None or value == "":
                    continue
                account_data["overrides"][field_name] = value

        catalogs[username] = account_data

    return passwords, catalogs


def _merge_account_catalog(
    global_catalog: dict[str, Any],
    project_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Merge one account catalog entry with project values overriding global values."""
    merged_projects = dict(global_catalog.get("projects", {}))
    merged_projects.update(project_catalog.get("projects", {}))

    merged_workspaces = dict(global_catalog.get("workspaces", {}))
    merged_workspaces.update(project_catalog.get("workspaces", {}))

    merged_project_catalog = dict(global_catalog.get("project_catalog", {}))
    merged_project_catalog.update(project_catalog.get("project_catalog", {}))

    global_overrides = global_catalog.get("overrides", {})
    project_overrides = project_catalog.get("overrides", {})
    merged_overrides = dict(global_overrides)
    merged_overrides.update(project_overrides)

    global_compute_groups = global_catalog.get("compute_groups", [])
    project_compute_groups = project_catalog.get("compute_groups", [])

    shared_path_group = project_catalog.get("shared_path_group")
    if not shared_path_group:
        shared_path_group = global_catalog.get("shared_path_group")

    train_job_workdir = project_catalog.get("train_job_workdir")
    if not train_job_workdir:
        train_job_workdir = global_catalog.get("train_job_workdir")

    return {
        "projects": merged_projects,
        "workspaces": merged_workspaces,
        "compute_groups": (
            project_compute_groups if project_compute_groups else list(global_compute_groups)
        ),
        "project_catalog": merged_project_catalog,
        "shared_path_group": shared_path_group,
        "train_job_workdir": train_job_workdir,
        "overrides": merged_overrides,
    }


def _merge_account_catalogs(
    global_catalogs: dict[str, dict[str, Any]],
    project_catalogs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge account catalogs keyed by username with project values overriding global values."""
    merged_catalogs: dict[str, dict[str, Any]] = {}
    usernames = set(global_catalogs.keys()) | set(project_catalogs.keys())

    for username in usernames:
        global_catalog = global_catalogs.get(username, {})
        project_catalog = project_catalogs.get(username, {})

        if global_catalog and project_catalog:
            merged_catalogs[username] = _merge_account_catalog(global_catalog, project_catalog)
        elif project_catalog:
            merged_catalogs[username] = project_catalog
        elif global_catalog:
            merged_catalogs[username] = global_catalog

    return merged_catalogs


def _apply_global_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
) -> tuple[Path | None, dict[str, dict[str, Any]]]:
    global_config_path: Path | None = None
    global_account_catalogs: dict[str, dict[str, Any]] = {}
    if not Config.GLOBAL_CONFIG_PATH.exists():
        return global_config_path, global_account_catalogs

    global_config_path = Config.GLOBAL_CONFIG_PATH
    global_raw = _load_toml(Config.GLOBAL_CONFIG_PATH)
    global_compute_groups = global_raw.pop("compute_groups", [])
    global_remote_env = {str(k): str(v) for k, v in global_raw.pop("remote_env", {}).items()}
    global_accounts, global_account_catalogs = _parse_global_accounts(
        global_raw.pop("accounts", {})
    )

    global_defaults: dict[str, Any] = {}
    raw_global_defaults = global_raw.pop("defaults", {})
    if isinstance(raw_global_defaults, dict):
        global_defaults = raw_global_defaults

    global_workspaces: dict[str, str] = {}
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

    _apply_defaults_overrides(
        defaults=global_defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_GLOBAL,
    )
    return global_config_path, global_account_catalogs


def _apply_project_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
) -> _ProjectLayerState:
    project_config_path = _find_project_config()
    layer_state = _ProjectLayerState(
        project_config_path=project_config_path,
        project_projects={},
        project_defaults={},
        project_context={},
        project_account_catalogs={},
        project_accounts={},
    )
    if not project_config_path:
        return layer_state

    project_raw = _load_toml(project_config_path)
    cli_section = project_raw.pop("cli", {})
    # prefer_source is intentionally project-scoped so each repo can choose
    # whether project TOML should override environment values.
    prefer_source = cli_section.get("prefer_source", "env")
    if prefer_source not in ("env", "toml"):
        raise ConfigError(
            f"Invalid prefer_source value: '{prefer_source}'\n"
            "Must be 'env' or 'toml' in [cli] section of project config."
        )
    layer_state.prefer_source = prefer_source

    project_compute_groups = project_raw.pop("compute_groups", [])
    project_remote_env = {str(k): str(v) for k, v in project_raw.pop("remote_env", {}).items()}
    project_projects = _parse_alias_map(project_raw.pop("projects", {}))
    layer_state.project_projects = project_projects

    raw_defaults = project_raw.pop("defaults", {})
    if isinstance(raw_defaults, dict):
        layer_state.project_defaults = raw_defaults
    raw_context = project_raw.pop("context", {})
    if isinstance(raw_context, dict):
        layer_state.project_context = raw_context

    project_accounts, project_account_catalogs = _parse_global_accounts(
        project_raw.pop("accounts", {})
    )
    layer_state.project_accounts = project_accounts
    layer_state.project_account_catalogs = project_account_catalogs

    project_workspaces: dict[str, str] = {}
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
    if project_accounts:
        merged_accounts = dict(config_dict.get("accounts", {}))
        merged_accounts.update(project_accounts)
        config_dict["accounts"] = merged_accounts
        sources["accounts"] = SOURCE_PROJECT

    return layer_state


def _apply_account_catalog_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    context_account: str,
    project_projects: dict[str, str],
    global_account_catalogs: dict[str, dict[str, Any]],
    project_account_catalogs: dict[str, dict[str, Any]],
) -> None:
    selected_account = (
        context_account
        or str(config_dict.get("username") or "").strip()
        or str(os.getenv("INSPIRE_USERNAME") or "").strip()
    )
    merged_account_catalogs = _merge_account_catalogs(
        global_account_catalogs, project_account_catalogs
    )
    account_catalog = merged_account_catalogs.get(selected_account, {})
    account_catalog_source = (
        SOURCE_PROJECT
        if selected_account and selected_account in project_account_catalogs
        else SOURCE_GLOBAL
    )

    account_projects = account_catalog.get("projects", {}) if account_catalog else {}
    account_workspaces = account_catalog.get("workspaces", {}) if account_catalog else {}
    account_compute_groups = account_catalog.get("compute_groups", []) if account_catalog else []
    account_project_catalog = account_catalog.get("project_catalog", {}) if account_catalog else {}
    account_shared_path_group = (
        account_catalog.get("shared_path_group") if account_catalog else None
    )
    account_train_job_workdir = (
        account_catalog.get("train_job_workdir") if account_catalog else None
    )
    account_overrides = account_catalog.get("overrides", {}) if account_catalog else {}

    if account_overrides:
        for field_name, value in account_overrides.items():
            if field_name not in config_dict:
                continue
            if sources.get(field_name) == SOURCE_PROJECT:
                continue
            config_dict[field_name] = value
            sources[field_name] = account_catalog_source

    if account_workspaces:
        merged_workspaces = dict(account_workspaces)
        merged_workspaces.update(config_dict.get("workspaces", {}))
        config_dict["workspaces"] = merged_workspaces
        if sources.get("workspaces") == SOURCE_DEFAULT:
            sources["workspaces"] = account_catalog_source

        if not config_dict.get("workspace_cpu_id") and merged_workspaces.get("cpu"):
            config_dict["workspace_cpu_id"] = merged_workspaces["cpu"]
            sources["workspace_cpu_id"] = account_catalog_source
        if not config_dict.get("workspace_gpu_id") and merged_workspaces.get("gpu"):
            config_dict["workspace_gpu_id"] = merged_workspaces["gpu"]
            sources["workspace_gpu_id"] = account_catalog_source
        if not config_dict.get("workspace_internet_id") and merged_workspaces.get("internet"):
            config_dict["workspace_internet_id"] = merged_workspaces["internet"]
            sources["workspace_internet_id"] = account_catalog_source

    merged_projects = dict(account_projects)
    merged_projects.update(project_projects)
    if merged_projects:
        config_dict["projects"] = merged_projects
        sources["projects"] = SOURCE_PROJECT if project_projects else account_catalog_source

    if isinstance(account_project_catalog, dict) and account_project_catalog:
        config_dict["project_catalog"] = account_project_catalog
        sources["project_catalog"] = account_catalog_source

        shared_groups: dict[str, str] = {}
        workdirs: dict[str, str] = {}
        for project_id, entry in account_project_catalog.items():
            if not isinstance(entry, dict):
                continue

            shared = str(entry.get("shared_path_group") or "").strip()
            if shared:
                shared_groups[str(project_id)] = shared

            workdir = str(entry.get("workdir") or "").strip()
            if workdir:
                workdirs[str(project_id)] = workdir

        if shared_groups:
            config_dict["project_shared_path_groups"] = shared_groups
            sources["project_shared_path_groups"] = account_catalog_source
        if workdirs:
            config_dict["project_workdirs"] = workdirs
            sources["project_workdirs"] = account_catalog_source

    if account_shared_path_group:
        config_dict["account_shared_path_group"] = str(account_shared_path_group)
        sources["account_shared_path_group"] = account_catalog_source
    if account_train_job_workdir:
        config_dict["account_train_job_workdir"] = str(account_train_job_workdir)
        sources["account_train_job_workdir"] = account_catalog_source

    if account_compute_groups and not config_dict.get("compute_groups"):
        config_dict["compute_groups"] = account_compute_groups
        sources["compute_groups"] = account_catalog_source


def _apply_project_context_and_defaults(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    context_account: str,
    project_context: dict[str, Any],
    project_defaults: dict[str, Any],
) -> None:
    if context_account and not config_dict.get("username"):
        config_dict["username"] = context_account
        sources["username"] = SOURCE_PROJECT

    project_ref = _resolve_alias(
        project_context.get("project"),
        config_dict.get("projects", {}),
        id_prefix="project-",
    )
    if project_ref:
        config_dict["job_project_id"] = project_ref
        sources["job_project_id"] = SOURCE_PROJECT

    for context_key, field_name in _CONTEXT_WORKSPACE_FIELD_MAP.items():
        workspace_ref = _resolve_alias(
            project_context.get(context_key),
            config_dict.get("workspaces", {}),
            id_prefix="ws-",
        )
        if not workspace_ref:
            continue
        config_dict[field_name] = workspace_ref
        sources[field_name] = SOURCE_PROJECT

    _apply_defaults_overrides(
        defaults=project_defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_PROJECT,
    )


def _apply_env_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    prefer_source: str,
) -> str | None:
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

        # In TOML-first mode, do not let env vars clobber project-layer values.
        if prefer_source == "toml" and sources.get(field_name) == SOURCE_PROJECT:
            continue

        config_dict[field_name] = new_value
        sources[field_name] = SOURCE_ENV

    return env_password


def _apply_password_and_token_fallbacks(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    project_accounts: dict[str, str],
    env_password: str | None,
) -> None:
    # Password fallback is intentionally layered:
    # 1) explicit config.password if already set
    # 2) account-password lookup keyed by resolved username
    # 3) INSPIRE_PASSWORD environment fallback
    if not config_dict.get("password"):
        resolved_username = str(config_dict.get("username") or "").strip()
        account_password = config_dict.get("accounts", {}).get(resolved_username)
        if account_password:
            config_dict["password"] = account_password
            sources["password"] = (
                SOURCE_PROJECT if resolved_username in project_accounts else SOURCE_GLOBAL
            )

    if not config_dict.get("password") and env_password:
        config_dict["password"] = env_password
        sources["password"] = SOURCE_ENV

    if not config_dict.get("github_token"):
        github_token_fallback = os.getenv("GITHUB_TOKEN")
        if github_token_fallback:
            config_dict["github_token"] = github_token_fallback
            sources["github_token"] = SOURCE_ENV


def _validate_required_config(
    *,
    config_dict: dict[str, Any],
    require_credentials: bool,
    require_target_dir: bool,
) -> None:
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
                "Set INSPIRE_PASSWORD env var or add an account password in config.toml:\n"
                '  [accounts."your_username"]\n'
                "  password = 'your_password'"
            )

    if require_target_dir and not config_dict["target_dir"]:
        raise ConfigError(
            "Missing target directory configuration.\n"
            "Set INSPIRE_TARGET_DIR env var or add to config.toml:\n"
            "  [paths]\n"
            "  target_dir = '/path/to/shared/directory'"
        )


def config_from_files_and_env(
    *,
    require_target_dir: bool = False,
    require_credentials: bool = True,
) -> tuple[Config, dict[str, str]]:
    """Load config from files + env vars with layered precedence."""
    config_dict = _default_config_values()
    sources = _initialize_sources(config_dict)
    global_config_path, global_account_catalogs = _apply_global_layer(
        config_dict=config_dict,
        sources=sources,
    )
    project_layer_state = _apply_project_layer(config_dict=config_dict, sources=sources)
    project_config_path = project_layer_state.project_config_path
    project_projects = project_layer_state.project_projects
    project_defaults = project_layer_state.project_defaults
    project_context = project_layer_state.project_context
    project_account_catalogs = project_layer_state.project_account_catalogs
    project_accounts = project_layer_state.project_accounts
    prefer_source = project_layer_state.prefer_source

    context_account = str(project_context.get("account") or "").strip()
    if context_account:
        config_dict["context_account"] = context_account
        sources["context_account"] = SOURCE_PROJECT

    _apply_account_catalog_layer(
        config_dict=config_dict,
        sources=sources,
        context_account=context_account,
        project_projects=project_projects,
        global_account_catalogs=global_account_catalogs,
        project_account_catalogs=project_account_catalogs,
    )
    _apply_project_context_and_defaults(
        config_dict=config_dict,
        sources=sources,
        context_account=context_account,
        project_context=project_context,
        project_defaults=project_defaults,
    )
    env_password = _apply_env_layer(
        config_dict=config_dict,
        sources=sources,
        prefer_source=prefer_source,
    )
    _apply_password_and_token_fallbacks(
        config_dict=config_dict,
        sources=sources,
        project_accounts=project_accounts,
        env_password=env_password,
    )
    _validate_required_config(
        config_dict=config_dict,
        require_credentials=require_credentials,
        require_target_dir=require_target_dir,
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
