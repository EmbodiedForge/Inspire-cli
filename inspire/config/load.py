"""Config file loading + merging for Inspire CLI."""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

from inspire.config.models import (
    Config,
    ConfigDeprecationWarning,
    ConfigError,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_INFERRED,
    SOURCE_PROJECT,
    SOURCE_CLI,
)
from inspire.config.accounts import get_account_override
from inspire.config.toml import _find_project_config

from .load_accounts import (
    _apply_account_catalog_layer,
    _apply_project_context_and_defaults,
    _merge_account_catalogs,
)
from .load_common import _default_config_values, _initialize_sources
from .load_layers import _apply_global_layer, _apply_project_layer
from .load_runtime import (
    _apply_env_layer,
    _apply_password_and_token_fallbacks,
    _validate_required_config,
)


def _should_show_deprecation_warning():
    return os.environ.get("INSPIRE_VERBOSE", "").lower() in ("1", "true", "yes")


def _warn_legacy_project_context_keys(
    *,
    project_context: dict[str, object],
    project_config_path: Path | None,
) -> None:
    if not str(project_context.get("project") or "").strip():
        return
    if _should_show_deprecation_warning():
        path_label = str(project_config_path) if project_config_path else "project config"
        warnings.warn(
            (
                f"{path_label} uses deprecated [context].project. Use "
                "[job].project_id or [defaults].project_order instead."
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

        if _should_show_deprecation_warning():
            warnings.warn(
                (
                    f"{location} uses deprecated workspace pin {subject}. "
                    'Prefer [accounts."<user>".workspaces] aliases plus --workspace, and reserve '
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
    account_alias: str | None = None,
) -> tuple[Config, dict[str, str]]:
    """Load config from files + env vars with layered precedence."""
    config_dict = _default_config_values()
    sources = _initialize_sources(config_dict)

    (
        global_config_path,
        global_account_catalogs,
        global_context,
        global_profiles,
    ) = _apply_global_layer(config_dict=config_dict, sources=sources)
    project_layer_state = _apply_project_layer(config_dict=config_dict, sources=sources)
    project_config_path = project_layer_state.project_config_path
    project_projects = project_layer_state.project_projects
    project_defaults = project_layer_state.project_defaults
    project_context = project_layer_state.project_context
    project_account_catalogs = project_layer_state.project_account_catalogs
    project_accounts = project_layer_state.project_accounts
    project_profiles = project_layer_state.project_profiles
    prefer_source = project_layer_state.prefer_source

    _warn_legacy_project_context_keys(
        project_context=project_context,
        project_config_path=project_config_path,
    )

    account_override = get_account_override()
    env_account = str(os.getenv("INSPIRE_ACCOUNT") or "").strip()
    project_account = str(project_context.get("account") or "").strip()
    global_account = str(global_context.get("account") or "").strip()
    if str(account_alias or "").strip():
        selected_account_alias = str(account_alias).strip()
        account_alias_source = SOURCE_CLI
    elif account_override is not None:
        selected_account_alias = account_override.alias
        account_alias_source = account_override.source
    elif env_account:
        selected_account_alias = env_account
        account_alias_source = SOURCE_ENV
    elif project_account:
        selected_account_alias = project_account
        account_alias_source = SOURCE_PROJECT
    else:
        selected_account_alias = global_account
        account_alias_source = SOURCE_GLOBAL

    env_username = str(os.getenv("INSPIRE_USERNAME") or "").strip()
    username_source = sources.get("username")
    if (
        env_username
        and not selected_account_alias
        and not (prefer_source == "toml" and username_source == SOURCE_PROJECT)
    ):
        # Warn when env var overrides config file
        if (
            username_source in (SOURCE_GLOBAL, SOURCE_PROJECT)
            and config_dict.get("username") != env_username
        ):
            print(
                f"[inspire-cli] Note: INSPIRE_USERNAME='{env_username}' overrides config file value '{config_dict.get('username')}'",
                file=sys.stderr,
            )
        config_dict["username"] = env_username
        sources["username"] = SOURCE_ENV

    current_username = str(config_dict.get("username") or "").strip()
    if not current_username and not selected_account_alias:
        merged_catalogs = _merge_account_catalogs(global_account_catalogs, project_account_catalogs)
        available_aliases = sorted(
            key for key, value in merged_catalogs.items() if value.get("username")
        )
        if available_aliases:
            raise ConfigError(
                "No account alias selected. Use --account, INSPIRE_ACCOUNT, or "
                "[context].account. Available: " + ", ".join(available_aliases)
            )
        candidate_accounts = {
            key for key, value in merged_catalogs.items() if not value.get("username")
        }
        if len(candidate_accounts) == 1:
            inferred_username = next(iter(candidate_accounts))
            config_dict["username"] = inferred_username
            sources["username"] = SOURCE_INFERRED
            if _should_show_deprecation_warning():
                warnings.warn(
                    (
                        "No explicit account was configured; inferred username "
                        f"'{inferred_username}' from a single [accounts] entry. "
                        "Set [auth].username or INSPIRE_USERNAME to avoid ambiguity."
                    ),
                    ConfigDeprecationWarning,
                    stacklevel=3,
                )

    _apply_account_catalog_layer(
        config_dict=config_dict,
        sources=sources,
        selected_account_alias=selected_account_alias,
        account_alias_source=account_alias_source,
        project_projects=project_projects,
        global_account_catalogs=global_account_catalogs,
        project_account_catalogs=project_account_catalogs,
    )
    _apply_project_context_and_defaults(
        config_dict=config_dict,
        sources=sources,
        project_context=project_context,
        project_defaults=project_defaults,
    )

    env_password = _apply_env_layer(
        config_dict=config_dict,
        sources=sources,
        prefer_source=prefer_source,
        protected_fields={"username"} if selected_account_alias else None,
    )
    _apply_password_and_token_fallbacks(
        config_dict=config_dict,
        sources=sources,
        project_accounts=project_accounts,
        env_password=env_password,
        prefer_source=prefer_source,
    )
    from inspire.config.workload_profiles import merge_profiles

    merged_profiles, profile_sources = merge_profiles(global_profiles, project_profiles)
    config_dict["profiles"] = merged_profiles
    config_dict["profile_sources"] = profile_sources
    sources["profiles"] = SOURCE_PROJECT if project_profiles else SOURCE_GLOBAL
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
