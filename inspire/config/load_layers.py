"""Global and project TOML layer helpers for config loading."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

from inspire.config.models import (
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    Config,
    ConfigDeprecationWarning,
    ConfigError,
)
from inspire.config.toml import (
    _find_project_config,
    _flatten_toml,
    _load_toml,
    _validate_toml_value,
)
from inspire.config.schema import get_option_by_toml

from .load_accounts import _parse_global_accounts
from .load_common import (
    _ProjectLayerState,
    _apply_defaults_overrides,
    _parse_alias_map,
)


def _parse_string_list(raw_value: Any, *, field_name: str, config_path: Path | None) -> list[str]:
    if isinstance(raw_value, str):
        return [raw_value]
    if isinstance(raw_value, list):
        values: list[str] = []
        for item in raw_value:
            if not isinstance(item, str):
                path_label = str(config_path) if config_path else "config.toml"
                raise ConfigError(
                    f"{path_label}: remote_runtime.{field_name} entries must be strings."
                )
            values.append(item)
        return values

    path_label = str(config_path) if config_path else "config.toml"
    raise ConfigError(
        f"{path_label}: remote_runtime.{field_name} must be a string or list of strings."
    )


def _apply_remote_runtime_section(
    *,
    raw_data: dict[str, Any],
    config_dict: dict[str, Any],
    sources: dict[str, str],
    source_name: str,
    config_path: Path | None,
) -> None:
    remote_runtime = raw_data.pop("remote_runtime", None)
    if remote_runtime is None:
        return
    if not isinstance(remote_runtime, dict):
        path_label = str(config_path) if config_path else f"{source_name} config"
        raise ConfigError(f"{path_label}: [remote_runtime] must be a table.")

    if "cwd" in remote_runtime:
        raw_cwd = remote_runtime.get("cwd")
        if raw_cwd is not None and raw_cwd != "":
            config_dict["remote_runtime_cwd"] = str(raw_cwd)
            sources["remote_runtime_cwd"] = source_name

    if "path_prepend" in remote_runtime:
        raw_paths = remote_runtime.get("path_prepend")
        if raw_paths not in (None, ""):
            config_dict["remote_runtime_path_prepend"] = _parse_string_list(
                raw_paths,
                field_name="path_prepend",
                config_path=config_path,
            )
            sources["remote_runtime_path_prepend"] = source_name

    if "venv" in remote_runtime:
        raw_venv = remote_runtime.get("venv")
        if raw_venv is not None and raw_venv != "":
            config_dict["remote_runtime_venv"] = str(raw_venv)
            sources["remote_runtime_venv"] = source_name

    raw_env = remote_runtime.get("env")
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            path_label = str(config_path) if config_path else f"{source_name} config"
            raise ConfigError(f"{path_label}: [remote_runtime.env] must be a table.")
        runtime_env = {str(k): str(v) for k, v in raw_env.items()}
        if source_name == SOURCE_PROJECT:
            merged_runtime_env = dict(config_dict.get("remote_runtime_env", {}))
            merged_runtime_env.update(runtime_env)
            config_dict["remote_runtime_env"] = merged_runtime_env
        else:
            config_dict["remote_runtime_env"] = runtime_env
        sources["remote_runtime_env"] = source_name


def _apply_legacy_paths_target_dir(
    *,
    raw_data: dict[str, Any],
    config_dict: dict[str, Any],
    sources: dict[str, str],
    source_name: str,
    config_path: Path | None,
) -> None:
    """Handle legacy paths.target_dir with fallback to defaults.target_dir."""
    # Check if defaults.target_dir is already set
    if config_dict.get("target_dir"):
        return

    # Check for legacy paths.target_dir
    paths_section = raw_data.get("paths")
    if not isinstance(paths_section, dict):
        return

    legacy_value = paths_section.get("target_dir")
    if legacy_value is None or legacy_value == "":
        return

    # Apply the legacy value
    config_dict["target_dir"] = str(legacy_value)
    sources["target_dir"] = source_name

    # Emit deprecation warning
    path_label = str(config_path) if config_path else f"{source_name} config"
    warnings.warn(
        f"{path_label} uses deprecated [paths].target_dir. "
        f"Use [defaults].target_dir instead. "
        f"The legacy key still works but will be removed in a future release.",
        ConfigDeprecationWarning,
        stacklevel=4,
    )


def _apply_legacy_workspace_id_section(
    *,
    raw_data: dict[str, Any],
    config_dict: dict[str, Any],
    sources: dict[str, str],
    source_name: str,
) -> None:
    for section_name, field_name in (
        ("job", "job_workspace_id"),
        ("notebook", "notebook_workspace_id"),
    ):
        section = raw_data.get(section_name)
        if not isinstance(section, dict):
            continue
        raw_value = section.get("workspace_id")
        if raw_value is None or raw_value == "":
            continue
        config_dict[field_name] = str(raw_value)
        sources[field_name] = source_name


def _apply_global_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
) -> tuple[Path | None, dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    global_config_path: Path | None = None
    global_account_catalogs: dict[str, dict[str, Any]] = {}
    global_context: dict[str, Any] = {}
    global_profiles: dict[str, Any] = {}
    resolved_global_path = Config.resolve_global_config_path()
    if not resolved_global_path.exists():
        return global_config_path, global_account_catalogs, global_context, global_profiles

    global_config_path = resolved_global_path
    global_raw = _load_toml(resolved_global_path)
    global_compute_groups = global_raw.pop("compute_groups", [])
    global_remote_env = {str(k): str(v) for k, v in global_raw.pop("remote_env", {}).items()}
    global_accounts, global_account_catalogs = _parse_global_accounts(
        global_raw.pop("accounts", {})
    )
    global_workspace_specs = global_raw.pop("workspace_specs", {})
    global_workspace_names = global_raw.pop("workspace_names", {})
    raw_global_context = global_raw.pop("context", {})
    if isinstance(raw_global_context, dict):
        global_context = raw_global_context
    raw_global_profiles = global_raw.pop("profiles", {})
    if isinstance(raw_global_profiles, dict):
        global_profiles = raw_global_profiles

    global_defaults: dict[str, Any] = {}
    raw_global_defaults = global_raw.pop("defaults", {})
    if isinstance(raw_global_defaults, dict):
        global_defaults = raw_global_defaults

    # Workspace aliases are account-scoped. Ignore legacy shared [workspaces]
    # during effective config loading.
    global_raw.pop("workspaces", None)
    _apply_remote_runtime_section(
        raw_data=global_raw,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_GLOBAL,
        config_path=global_config_path,
    )

    flat_global = _flatten_toml(global_raw)
    for toml_key, value in flat_global.items():
        option = get_option_by_toml(toml_key)
        if not option or option.field_name not in config_dict:
            continue
        config_dict[option.field_name] = _validate_toml_value(option, value)
        sources[option.field_name] = SOURCE_GLOBAL

    if global_compute_groups:
        config_dict["compute_groups"] = global_compute_groups
        sources["compute_groups"] = SOURCE_GLOBAL
    if global_remote_env:
        config_dict["remote_env"] = global_remote_env
        sources["remote_env"] = SOURCE_GLOBAL
    if global_accounts:
        config_dict["accounts"] = global_accounts
        sources["accounts"] = SOURCE_GLOBAL
    if global_workspace_specs:
        config_dict["workspace_specs"] = global_workspace_specs
        sources["workspace_specs"] = SOURCE_GLOBAL
    if global_workspace_names:
        config_dict["workspace_names"] = global_workspace_names
        sources["workspace_names"] = SOURCE_GLOBAL

    _apply_legacy_workspace_id_section(
        raw_data=global_raw,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_GLOBAL,
    )

    _apply_defaults_overrides(
        defaults=global_defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_GLOBAL,
    )

    _apply_legacy_paths_target_dir(
        raw_data=global_raw,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_GLOBAL,
        config_path=global_config_path,
    )

    return global_config_path, global_account_catalogs, global_context, global_profiles


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
        project_profiles={},
    )
    if not project_config_path:
        return layer_state

    project_raw = _load_toml(project_config_path)
    cli_section = project_raw.pop("cli", {})
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
    raw_profiles = project_raw.pop("profiles", {})
    if isinstance(raw_profiles, dict):
        layer_state.project_profiles = raw_profiles

    project_accounts, project_account_catalogs = _parse_global_accounts(
        project_raw.pop("accounts", {})
    )
    layer_state.project_accounts = project_accounts
    layer_state.project_account_catalogs = project_account_catalogs

    # Workspace aliases are account-scoped. Ignore legacy shared [workspaces]
    # during effective config loading.
    project_raw.pop("workspaces", None)
    _apply_remote_runtime_section(
        raw_data=project_raw,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_PROJECT,
        config_path=project_config_path,
    )

    flat_project = _flatten_toml(project_raw)
    for toml_key, value in flat_project.items():
        option = get_option_by_toml(toml_key)
        if not option or option.field_name not in config_dict:
            continue
        config_dict[option.field_name] = _validate_toml_value(option, value)
        sources[option.field_name] = SOURCE_PROJECT

    if project_compute_groups:
        config_dict["compute_groups"] = project_compute_groups
        sources["compute_groups"] = SOURCE_PROJECT
    if project_remote_env:
        merged_remote_env = dict(config_dict.get("remote_env", {}))
        merged_remote_env.update(project_remote_env)
        config_dict["remote_env"] = merged_remote_env
        sources["remote_env"] = SOURCE_PROJECT
    if project_accounts:
        merged_accounts = dict(config_dict.get("accounts", {}))
        merged_accounts.update(project_accounts)
        config_dict["accounts"] = merged_accounts
        sources["accounts"] = SOURCE_PROJECT

    _apply_legacy_workspace_id_section(
        raw_data=project_raw,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_PROJECT,
    )

    _apply_legacy_paths_target_dir(
        raw_data=project_raw,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_PROJECT,
        config_path=project_config_path,
    )

    return layer_state


__all__ = ["_apply_global_layer", "_apply_project_layer"]
