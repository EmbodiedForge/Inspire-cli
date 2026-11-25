"""Account catalog parsing and merge helpers for config loading."""

from __future__ import annotations

from typing import Any

from inspire.config.models import ConfigError, SOURCE_DEFAULT, SOURCE_GLOBAL, SOURCE_PROJECT
from inspire.config.account_state import (
    get_account_discovery_state_path,
    load_account_discovery_state,
)

from .load_common import (
    _ACCOUNT_OVERRIDE_FIELDS,
    _ACCOUNT_SECTION_KEY_MAP,
    _CONTEXT_WORKSPACE_FIELD_MAP,
    _apply_defaults_overrides,
    _normalize_compute_groups,
    _normalize_project_catalog,
    _resolve_alias,
)
from inspire.config.schema import get_option_by_field
from inspire.config.toml import _validate_toml_value


def _validated_override(field_name: str, value: Any) -> Any:
    """Validate a TOML account override value against its schema option."""
    opt = get_option_by_field(field_name)
    return _validate_toml_value(opt, value) if opt else value


def _parse_selector_maps(
    raw_value: object,
    *,
    id_prefix: str,
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    legacy: dict[str, str] = {}
    selectors: dict[str, dict[str, str]] = {}
    if not isinstance(raw_value, dict):
        return legacy, selectors
    for raw_alias, raw_selector in raw_value.items():
        alias = str(raw_alias or "").strip()
        if not alias:
            continue
        if isinstance(raw_selector, str):
            value = raw_selector.strip()
            legacy_prefixes = (id_prefix, "proj-") if id_prefix == "project-" else (id_prefix,)
            if value.startswith(legacy_prefixes):
                legacy[alias] = value
            elif value:
                selectors[alias] = {"name": value}
            continue
        if not isinstance(raw_selector, dict):
            continue
        name = str(raw_selector.get("name") or "").strip()
        workspace = str(raw_selector.get("workspace") or "").strip()
        if name:
            selectors[alias] = {"name": name}
            if workspace:
                selectors[alias]["workspace"] = workspace
    return legacy, selectors


def _parse_pool_internet(raw_pools: Any) -> dict[str, bool]:
    if not isinstance(raw_pools, dict):
        return {}
    declarations: dict[str, bool] = {}
    for pool_id, raw_entry in raw_pools.items():
        if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("internet"), bool):
            continue
        declarations[str(pool_id)] = raw_entry["internet"]
    return declarations


def _parse_global_accounts(raw_accounts: Any) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Parse account records keyed by alias, plus legacy username-keyed records."""
    if not isinstance(raw_accounts, dict):
        return {}, {}

    passwords: dict[str, str] = {}
    catalogs: dict[str, dict[str, Any]] = {}
    for raw_alias, raw_value in raw_accounts.items():
        alias = str(raw_alias).strip()
        if not alias or not isinstance(raw_value, dict):
            continue

        legacy_projects, project_selectors = _parse_selector_maps(
            raw_value.get("projects", {}), id_prefix="project-"
        )
        _, workspace_selectors = _parse_selector_maps(
            raw_value.get("workspaces", {}), id_prefix="ws-"
        )
        account_data: dict[str, Any] = {
            "username": str(raw_value.get("username") or "").strip() or None,
            "password": str(raw_value.get("password") or "") or None,
            "password_command": str(raw_value.get("password_command") or "").strip() or None,
            "projects": legacy_projects,
            "project_selectors": project_selectors,
            "workspace_selectors": workspace_selectors,
            "pool_internet": _parse_pool_internet(raw_value.get("pools", {})),
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
                passwords[alias] = password_str

        for field_name in _ACCOUNT_OVERRIDE_FIELDS:
            value = raw_value.get(field_name)
            if value is None or value == "":
                continue
            account_data["overrides"][field_name] = _validated_override(field_name, value)

        for section_name, key_map in _ACCOUNT_SECTION_KEY_MAP.items():
            section = raw_value.get(section_name)
            if not isinstance(section, dict):
                continue
            for key, field_name in key_map.items():
                value = section.get(key)
                if value is None or value == "":
                    continue
                account_data["overrides"][field_name] = _validated_override(field_name, value)

        catalogs[alias] = account_data

    return passwords, catalogs


def _merge_account_catalog(
    global_catalog: dict[str, Any],
    project_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Merge one account catalog entry with project values overriding global values."""
    merged_projects = dict(global_catalog.get("projects", {}))
    merged_projects.update(project_catalog.get("projects", {}))

    merged_project_catalog = dict(global_catalog.get("project_catalog", {}))
    merged_project_catalog.update(project_catalog.get("project_catalog", {}))
    merged_workspace_selectors = dict(global_catalog.get("workspace_selectors", {}))
    merged_workspace_selectors.update(project_catalog.get("workspace_selectors", {}))
    merged_project_selectors = dict(global_catalog.get("project_selectors", {}))
    merged_project_selectors.update(project_catalog.get("project_selectors", {}))
    merged_pool_internet = dict(global_catalog.get("pool_internet", {}))
    merged_pool_internet.update(project_catalog.get("pool_internet", {}))

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
        "username": project_catalog.get("username") or global_catalog.get("username"),
        "password": project_catalog.get("password") or global_catalog.get("password"),
        "password_command": project_catalog.get("password_command")
        or global_catalog.get("password_command"),
        "projects": merged_projects,
        "compute_groups": (
            project_compute_groups if project_compute_groups else list(global_compute_groups)
        ),
        "project_catalog": merged_project_catalog,
        "workspace_selectors": merged_workspace_selectors,
        "project_selectors": merged_project_selectors,
        "pool_internet": merged_pool_internet,
        "shared_path_group": shared_path_group,
        "train_job_workdir": train_job_workdir,
        "overrides": merged_overrides,
    }


def _merge_account_catalogs(
    global_catalogs: dict[str, dict[str, Any]],
    project_catalogs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge account catalogs keyed by local alias or legacy username."""
    merged_catalogs: dict[str, dict[str, Any]] = {}
    aliases = set(global_catalogs.keys()) | set(project_catalogs.keys())

    for alias in aliases:
        global_catalog = global_catalogs.get(alias, {})
        project_catalog = project_catalogs.get(alias, {})

        if global_catalog and project_catalog:
            merged_catalogs[alias] = _merge_account_catalog(global_catalog, project_catalog)
        elif project_catalog:
            merged_catalogs[alias] = project_catalog
        elif global_catalog:
            merged_catalogs[alias] = global_catalog

    return merged_catalogs


def _apply_account_catalog_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    selected_account_alias: str,
    account_alias_source: str,
    project_projects: dict[str, str],
    global_account_catalogs: dict[str, dict[str, Any]],
    project_account_catalogs: dict[str, dict[str, Any]],
) -> None:
    merged_account_catalogs = _merge_account_catalogs(
        global_account_catalogs, project_account_catalogs
    )
    if selected_account_alias:
        if selected_account_alias not in merged_account_catalogs:
            available = ", ".join(sorted(merged_account_catalogs)) or "<none>"
            raise ConfigError(
                f"Account alias '{selected_account_alias}' is not configured. Available: {available}"
            )
        account_catalog = merged_account_catalogs[selected_account_alias]
        username = str(account_catalog.get("username") or "").strip()
        if not username:
            raise ConfigError(
                f"Account alias '{selected_account_alias}' must define username; "
                "account aliases are never inferred from platform usernames."
            )
        config_dict["account_alias"] = selected_account_alias
        config_dict["context_account"] = selected_account_alias
        config_dict["username"] = username
        sources["account_alias"] = account_alias_source
        sources["context_account"] = account_alias_source
        project_account = project_account_catalogs.get(selected_account_alias, {})
        sources["username"] = SOURCE_PROJECT if project_account.get("username") else SOURCE_GLOBAL
        password = str(account_catalog.get("password") or "")
        if password:
            config_dict["password"] = password
            sources["password"] = (
                SOURCE_PROJECT if project_account.get("password") else SOURCE_GLOBAL
            )
        password_command = str(account_catalog.get("password_command") or "").strip()
        if password_command:
            config_dict["password_command"] = password_command
            sources["password_command"] = (
                SOURCE_PROJECT if project_account.get("password_command") else SOURCE_GLOBAL
            )

        # Discovery state belongs to the selected account. Legacy top-level
        # caches may have been written by a different account, so an empty
        # account catalog must remain empty instead of inheriting those values.
        for field_name, empty_value in (
            ("compute_groups", []),
            ("workspace_specs", {}),
            ("workspace_names", {}),
        ):
            config_dict[field_name] = empty_value
            sources[field_name] = SOURCE_DEFAULT
        selected_account = selected_account_alias
    else:
        selected_account = str(config_dict.get("username") or "").strip()
        account_catalog = merged_account_catalogs.get(selected_account, {})

    account_catalog_source = (
        SOURCE_PROJECT
        if selected_account and selected_account in project_account_catalogs
        else SOURCE_GLOBAL
    )

    account_projects = account_catalog.get("projects", {}) if account_catalog else {}
    workspace_selectors = account_catalog.get("workspace_selectors", {}) if account_catalog else {}
    project_selectors = account_catalog.get("project_selectors", {}) if account_catalog else {}
    pool_internet = account_catalog.get("pool_internet", {}) if account_catalog else {}
    account_compute_groups = account_catalog.get("compute_groups", []) if account_catalog else []
    account_project_catalog = account_catalog.get("project_catalog", {}) if account_catalog else {}
    account_shared_path_group = (
        account_catalog.get("shared_path_group") if account_catalog else None
    )
    account_train_job_workdir = (
        account_catalog.get("train_job_workdir") if account_catalog else None
    )
    account_overrides = account_catalog.get("overrides", {}) if account_catalog else {}
    project_account_overrides = (
        project_account_catalogs.get(selected_account, {}).get("overrides", {})
        if selected_account
        else {}
    )

    if account_overrides:
        for field_name, value in account_overrides.items():
            if field_name not in config_dict:
                continue
            if sources.get(field_name) == SOURCE_PROJECT:
                continue
            config_dict[field_name] = value
            sources[field_name] = (
                SOURCE_PROJECT if field_name in project_account_overrides else SOURCE_GLOBAL
            )

    if pool_internet:
        config_dict["pool_internet"] = dict(pool_internet)
        sources["pool_internet"] = account_catalog_source

    config_dict["workspace_selectors"] = dict(workspace_selectors)
    config_dict["project_selectors"] = dict(project_selectors)
    sources["workspace_selectors"] = account_catalog_source
    sources["project_selectors"] = account_catalog_source

    discovery_state = None
    if selected_account_alias:
        discovery_state = load_account_discovery_state(selected_account_alias)
        if discovery_state is not None:
            config_dict["discovery_state_status"] = discovery_state.status
            config_dict["discovery_state_updated_at"] = discovery_state.updated_at
            config_dict["discovery_state_path"] = str(
                get_account_discovery_state_path(selected_account_alias)
            )
            if discovery_state.workspaces.status != "missing":
                config_dict["compute_groups"] = discovery_state.flattened_compute_groups()
                config_dict["workspace_specs"] = discovery_state.flattened_workspace_specs()
                config_dict["workspace_names"] = discovery_state.workspace_names()
                account_project_catalog = discovery_state.project_catalog()
                account_compute_groups = config_dict["compute_groups"]
                sources["compute_groups"] = account_catalog_source
                sources["workspace_specs"] = account_catalog_source
                sources["workspace_names"] = account_catalog_source

                discovered_shared_groups = {
                    str(entry.get("shared_path_group") or "").strip()
                    for entry in account_project_catalog.values()
                    if isinstance(entry, dict) and str(entry.get("shared_path_group") or "").strip()
                }
                if len(discovered_shared_groups) == 1:
                    account_shared_path_group = next(iter(discovered_shared_groups))
            elif discovery_state.workspace_specs:
                config_dict["workspace_specs"] = discovery_state.flattened_workspace_specs()
                sources["workspace_specs"] = account_catalog_source

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

    if account_compute_groups:
        config_dict["compute_groups"] = account_compute_groups
        sources["compute_groups"] = account_catalog_source


def _apply_project_context_and_defaults(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    project_context: dict[str, Any],
    project_defaults: dict[str, Any],
) -> None:
    raw_project = str(project_context.get("project") or "").strip()
    project_ref: str | None
    if raw_project in config_dict.get("project_selectors", {}):
        project_ref = raw_project
    else:
        project_ref = _resolve_alias(
            raw_project,
            config_dict.get("projects", {}),
            id_prefix="project-",
        )
    if project_ref and not config_dict.get("job_project_id"):
        config_dict["job_project_id"] = project_ref
        sources["job_project_id"] = SOURCE_PROJECT

    for context_key, field_name in _CONTEXT_WORKSPACE_FIELD_MAP.items():
        raw_workspace = str(project_context.get(context_key) or "").strip()
        workspace_ref: str | None
        if raw_workspace in config_dict.get("workspace_selectors", {}):
            workspace_ref = raw_workspace
        else:
            workspace_ref = _resolve_alias(raw_workspace, {}, id_prefix="ws-")
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


__all__ = [
    "_apply_account_catalog_layer",
    "_apply_project_context_and_defaults",
    "_merge_account_catalog",
    "_merge_account_catalogs",
    "_parse_global_accounts",
]
