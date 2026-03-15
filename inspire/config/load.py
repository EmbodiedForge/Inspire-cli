"""Config file loading + merging for Inspire CLI."""

from __future__ import annotations

import warnings
from pathlib import Path

from inspire.config.models import (
    Config,
    ConfigDeprecationWarning,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
)
from inspire.config.toml import _find_project_config

from .load_accounts import (
    _apply_account_catalog_layer,
    _apply_project_context_and_defaults,
)
from .load_common import _default_config_values, _initialize_sources
from .load_layers import _apply_global_layer, _apply_project_layer
from .load_runtime import (
    _apply_env_layer,
    _apply_password_and_token_fallbacks,
    _validate_required_config,
)


def _warn_legacy_project_context_keys(
    *,
    project_context: dict[str, object],
    project_config_path: Path | None,
) -> None:
    legacy_keys: list[str] = []
    if str(project_context.get("account") or "").strip():
        legacy_keys.append("[context].account")
    if str(project_context.get("project") or "").strip():
        legacy_keys.append("[context].project")
    for key in ("workspace", "workspace_cpu", "workspace_gpu", "workspace_internet"):
        if str(project_context.get(key) or "").strip():
            legacy_keys.append(f"[context].{key}")
    if not legacy_keys:
        return

    path_label = str(project_config_path) if project_config_path else "project config"
    replacements: list[str] = []
    if "[context].account" in legacy_keys:
        replacements.append("[auth].username")
    if "[context].project" in legacy_keys:
        replacements.append("[job].project_id or [defaults].project_order")
    if any(key.startswith("[context].workspace") for key in legacy_keys):
        replacements.append("[workspaces] aliases plus --workspace")
    replacements_label = " and ".join(replacements)

    warnings.warn(
        (
            f"{path_label} uses deprecated legacy keys {', '.join(legacy_keys)}. "
            f"Use {replacements_label} instead. Legacy keys still work for now, "
            "but will be removed in a future release."
        ),
        ConfigDeprecationWarning,
        stacklevel=3,
    )


def _warn_legacy_workspace_id_keys(
    *,
    config_dict: dict[str, object],
    sources: dict[str, str],
    global_config_path: Path | None,
    project_config_path: Path | None,
) -> None:
    key_specs = (
        ("default_workspace_id", "[defaults].workspace_id", "INSPIRE_DEFAULT_WORKSPACE_ID"),
        ("job_workspace_id", "[job].workspace_id", "INSPIRE_WORKSPACE_ID"),
        (
            "notebook_workspace_id",
            "[notebook].workspace_id",
            "INSPIRE_NOTEBOOK_WORKSPACE_ID",
        ),
    )
    for field_name, toml_key, env_var in key_specs:
        value = str(config_dict.get(field_name) or "").strip()
        source = sources.get(field_name)
        if not value or source not in {SOURCE_GLOBAL, SOURCE_PROJECT, SOURCE_ENV}:
            continue

        if source == SOURCE_PROJECT:
            location = str(project_config_path) if project_config_path else "project config"
            subject = toml_key
        elif source == SOURCE_GLOBAL:
            location = str(global_config_path) if global_config_path else "global config"
            subject = toml_key
        else:
            location = "environment"
            subject = env_var

        warnings.warn(
            (
                f"{location} uses deprecated workspace pin {subject}. "
                "Prefer [workspaces] aliases plus --workspace, and reserve "
                "--workspace-id for one-off explicit overrides. Legacy workspace_id "
                "pins still work for now, but will be removed in a future release."
            ),
            ConfigDeprecationWarning,
            stacklevel=3,
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

    _warn_legacy_project_context_keys(
        project_context=project_context,
        project_config_path=project_config_path,
    )

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
    _warn_legacy_workspace_id_keys(
        config_dict=config_dict,
        sources=sources,
        global_config_path=global_config_path,
        project_config_path=project_config_path,
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
    resolved_global_path = Config.resolve_global_config_path()
    global_path = resolved_global_path if resolved_global_path.exists() else None
    project_path = _find_project_config()
    return global_path, project_path
