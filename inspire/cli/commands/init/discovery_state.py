"""Build and reconcile account discovery snapshots for init."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import re
from typing import Any, Mapping

from inspire.config.account_state import AccountDiscoveryState, Observation
from inspire.platform.web.session import SessionExpiredError


def _slug(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return normalized or fallback


def _unique_alias(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _project_item(project: object) -> dict[str, Any]:
    if isinstance(project, Mapping):
        raw = dict(project)
    elif is_dataclass(project):
        if isinstance(project, type):
            raise TypeError("asdict() should be called on dataclass instances")
        raw = asdict(project)
    else:
        raw = dict(vars(project)) if hasattr(project, "__dict__") else {}
    project_id = str(raw.pop("project_id", "") or raw.get("id") or "").strip()
    raw["id"] = project_id
    raw["name"] = str(raw.get("name") or "").strip()
    raw["workspace_id"] = str(raw.get("workspace_id") or "").strip()
    return {key: value for key, value in raw.items() if value not in (None, "")}


def _compute_group_items(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> list[dict[str, Any]]:
    raw_groups = browser_api_module.list_compute_groups(
        workspace_id=workspace_id,
        session=session,
    )
    gpu_types: dict[str, str] = {}
    try:
        availability = browser_api_module.get_accurate_gpu_availability(
            workspace_id=workspace_id,
            session=session,
        )
        gpu_types = {
            str(item.group_id): str(item.gpu_type)
            for item in availability
            if getattr(item, "group_id", None)
        }
    except Exception:
        gpu_types = {}

    items: list[dict[str, Any]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, Mapping):
            continue
        item = dict(raw_group)
        group_id = str(
            item.get("logic_compute_group_id")
            or item.get("compute_group_id")
            or item.get("id")
            or ""
        ).strip()
        name = str(item.get("name") or item.get("compute_group_name") or "").strip()
        if not group_id or not name:
            continue
        item["id"] = group_id
        item["name"] = name
        gpu_type = str(gpu_types.get(group_id) or item.get("gpu_type") or "").strip()
        if gpu_type:
            item["gpu_type"] = gpu_type
        items.append(item)
    return items


def _workspace_spec_items(workspace_id: str, *, session) -> list[dict[str, Any]]:  # noqa: ANN001
    from inspire.platform.openapi.workspace_specs import fetch_workspace_specs

    specs = fetch_workspace_specs(workspace_id, session=session)
    return [
        {
            "spec_id": spec.spec_id,
            "gpu_type": (
                spec.gpu_type.value if hasattr(spec.gpu_type, "value") else str(spec.gpu_type)
            ),
            "gpu_count": spec.gpu_count,
            "cpu_cores": spec.cpu_cores,
            "memory_gb": spec.memory_gb,
            "gpu_memory_gb": spec.gpu_memory_gb,
            "description": spec.description,
        }
        for spec in specs
    ]


def observe_account_discovery(
    *,
    account_alias: str,
    platform_username: str,
    base_url: str,
    session,  # noqa: ANN001
    browser_api_module,  # noqa: ANN001
    fallback_workspace_id: str,
) -> AccountDiscoveryState:
    """Observe all discoverable account scopes without mutating local state."""
    from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

    workspace_items: list[dict[str, Any]] = []
    workspace_error: str | None = None
    workspace_observed = False
    try:
        enumerated = try_enumerate_workspaces(
            session,
            workspace_id=fallback_workspace_id,
            base_url=base_url,
        )
        workspace_items = [
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "").strip(),
            }
            for item in enumerated
            if isinstance(item, Mapping) and str(item.get("id") or "").strip()
        ]
        workspace_observed = True
    except SessionExpiredError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserved in state diagnostics
        workspace_error = str(exc)

    if not workspace_observed:
        ids = list(getattr(session, "all_workspace_ids", None) or [])
        names = dict(getattr(session, "all_workspace_names", None) or {})
        workspace_items = [
            {"id": str(workspace_id), "name": str(names.get(workspace_id) or "")}
            for workspace_id in ids
            if str(workspace_id or "").strip()
        ]
    if not workspace_observed and not workspace_items and fallback_workspace_id:
        workspace_items = [{"id": fallback_workspace_id, "name": ""}]

    workspaces = (
        Observation.success(workspace_items)
        if workspace_observed
        else Observation.failed(workspace_error or "No workspaces were returned")
    )
    state = AccountDiscoveryState(
        account_alias=account_alias,
        platform_username=platform_username,
        base_url=base_url,
        workspaces=workspaces,
    )

    for workspace in workspace_items:
        workspace_id = str(workspace.get("id") or "").strip()
        if not workspace_id:
            continue
        try:
            projects = browser_api_module.list_projects(
                workspace_id=workspace_id,
                session=session,
            )
            state.projects[workspace_id] = Observation.success(
                [_project_item(project) for project in projects]
            )
        except SessionExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.projects[workspace_id] = Observation.failed(str(exc))

        try:
            state.compute_groups[workspace_id] = Observation.success(
                _compute_group_items(
                    browser_api_module=browser_api_module,
                    session=session,
                    workspace_id=workspace_id,
                )
            )
        except SessionExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.compute_groups[workspace_id] = Observation.failed(str(exc))

        try:
            state.workspace_specs[workspace_id] = Observation.success(
                _workspace_spec_items(workspace_id, session=session)
            )
        except SessionExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.workspace_specs[workspace_id] = Observation.failed(str(exc))

    state.refresh_status()
    return state


def build_workspace_selectors(
    *,
    raw_workspaces: object,
    state: AccountDiscoveryState,
    previous_state: AccountDiscoveryState | None,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    del previous_state
    workspace_items = state.workspace_items()
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in workspace_items:
        by_name.setdefault(str(item.get("name") or ""), []).append(item)

    raw_map = raw_workspaces if isinstance(raw_workspaces, Mapping) else {}
    selectors: dict[str, dict[str, str]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for raw_alias, raw_selector in raw_map.items():
        alias = str(raw_alias or "").strip()
        if not alias:
            continue
        if isinstance(raw_selector, Mapping):
            name = str(raw_selector.get("name") or "").strip()
        elif isinstance(raw_selector, str) and not raw_selector.strip().startswith("ws-"):
            name = raw_selector.strip()
        else:
            name = ""
        if not name:
            errors.append(f"workspace alias {alias!r}: set a platform display name selector")
            continue

        matches = by_name.get(name, [])
        if len(matches) != 1:
            reason = "is ambiguous" if matches else "is not visible"
            errors.append(f"workspace alias {alias!r}: workspace name {name!r} {reason}")
            selectors[alias] = {"name": name}
            continue

        workspace_id = str(matches[0].get("id") or "")
        selectors[alias] = {"name": name}
        resolutions[alias] = {
            "name": name,
            "last_seen_id": workspace_id,
            "status": "resolved",
            "managed": False,
        }

    state.workspace_aliases = resolutions
    return selectors, errors


def build_project_selectors(
    *,
    raw_projects: object,
    state: AccountDiscoveryState,
    previous_state: AccountDiscoveryState | None,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    workspace_alias_for_id: dict[str, str] = {}
    for alias in sorted(state.workspace_aliases):
        resolution = state.workspace_aliases[alias]
        workspace_id = str(resolution.get("last_seen_id") or "").strip()
        if workspace_id:
            workspace_alias_for_id.setdefault(workspace_id, alias)

    projects_by_id: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    projects_by_scope_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for workspace_id, observation in state.projects.items():
        for item in observation.items:
            project_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            if project_id:
                projects_by_id.setdefault(project_id, []).append((workspace_id, dict(item)))
            projects_by_scope_name.setdefault((workspace_id, name), []).append(dict(item))

    def _preferred_project_scope(
        matches: list[tuple[str, dict[str, Any]]],
    ) -> tuple[str, dict[str, Any]] | None:
        if not matches:
            return None

        def _rank(match: tuple[str, dict[str, Any]]) -> tuple[str, str]:
            workspace_id, item = match
            workspace_alias = workspace_alias_for_id.get(workspace_id, "")
            return workspace_alias, str(item.get("id") or "")

        return min(matches, key=_rank)

    raw_map = raw_projects if isinstance(raw_projects, Mapping) else {}
    selectors: dict[str, dict[str, str]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for raw_alias, raw_selector in raw_map.items():
        alias = str(raw_alias or "").strip()
        if not alias:
            continue
        name = ""
        workspace_alias = ""
        legacy_id = ""
        if isinstance(raw_selector, str):
            if raw_selector.startswith("project-"):
                legacy_id = raw_selector
            else:
                name = raw_selector.strip()
        elif isinstance(raw_selector, Mapping):
            name = str(raw_selector.get("name") or "").strip()
            workspace_alias = str(raw_selector.get("workspace") or "").strip()

        previous_resolution = (
            (previous_state.project_aliases or {}).get(alias) or {} if previous_state else {}
        )
        managed = bool(previous_resolution.get("managed"))
        project_id = legacy_id or str(previous_resolution.get("last_seen_id") or "").strip()
        match: tuple[str, dict[str, Any]] | None = None
        error: str | None = None
        if name and workspace_alias:
            workspace_resolution = state.workspace_aliases.get(workspace_alias) or {}
            workspace_id = str(workspace_resolution.get("last_seen_id") or "")
            matches = projects_by_scope_name.get((workspace_id, name), [])
            if len(matches) == 1:
                match = (workspace_id, matches[0])
                project_id = str(matches[0].get("id") or "")
            elif len(matches) > 1:
                error = f"project name {name!r} is ambiguous"
            elif not project_id:
                error = f"project name {name!r} is not visible"

        if not match and project_id and not error:
            candidates = projects_by_id.get(project_id, [])
            if name and workspace_alias:
                workspace_id = str(
                    (state.workspace_aliases.get(workspace_alias) or {}).get("last_seen_id") or ""
                )
                same_workspace = [item for item in candidates if item[0] == workspace_id]
                match = _preferred_project_scope(same_workspace)
                if not match and not managed:
                    error = f"project name {name!r} is not visible"
            if not match and not error:
                match = _preferred_project_scope(candidates)

        if match:
            workspace_id, item = match
            workspace_alias = workspace_alias_for_id.get(workspace_id, workspace_alias)
            name = str(item.get("name") or name)
        else:
            error = error or "selector could not be resolved"

        if error:
            # Mirror the workspace side: a managed alias that stopped resolving
            # keeps its previous selector and surfaces an UNRESOLVED diagnostic;
            # silently dropping it would delete the config key with an empty
            # diff and no error. A bare string selector carries no workspace and
            # a legacy ID carries neither field, so both fall back to the
            # previously recorded selector.
            errors.append(f"project alias {alias!r}: {error}")
            name = name or str(previous_resolution.get("name") or "").strip()
            workspace_alias = (
                workspace_alias or str(previous_resolution.get("workspace") or "").strip()
            )

        if name and workspace_alias:
            selectors[alias] = {"workspace": workspace_alias, "name": name}
        if match and project_id and name and workspace_alias:
            resolutions[alias] = {
                "workspace": workspace_alias,
                "name": name,
                "last_seen_id": project_id,
                "status": "resolved",
                "managed": managed,
            }

    existing_aliases = set(selectors)
    resolved_ids = {str(item.get("last_seen_id") or "") for item in resolutions.values()}
    for project_id, project_matches in projects_by_id.items():
        if project_id in resolved_ids:
            continue
        preferred_match = _preferred_project_scope(project_matches)
        if preferred_match is None:
            continue
        workspace_id, item = preferred_match
        resolved_workspace_alias = workspace_alias_for_id.get(workspace_id)
        if not resolved_workspace_alias:
            continue
        name = str(item.get("name") or "").strip()
        alias = _unique_alias(_slug(name, fallback=project_id[8:16]), existing_aliases)
        existing_aliases.add(alias)
        selectors[alias] = {"workspace": resolved_workspace_alias, "name": name}
        resolutions[alias] = {
            "workspace": resolved_workspace_alias,
            "name": name,
            "last_seen_id": project_id,
            "status": "resolved",
            "managed": True,
        }

    state.project_aliases = resolutions
    return selectors, errors


def state_diff(
    previous: AccountDiscoveryState | None,
    current: AccountDiscoveryState,
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []

    def _ids(observation: Observation, *keys: str) -> set[str]:
        return {
            next((str(item.get(key) or "") for key in keys if item.get(key)), "")
            for item in observation.items
        } - {""}

    if current.workspaces.status == "failed":
        changes.append({"action": "PRESERVE_STALE", "scope": "workspaces", "id": "account"})

    prior_workspaces = _ids(previous.workspaces, "id") if previous else set()
    current_workspaces = _ids(current.workspaces, "id")
    for value in sorted(current_workspaces - prior_workspaces):
        changes.append({"action": "ADD", "scope": "workspace", "id": value})
    for value in sorted(prior_workspaces - current_workspaces):
        changes.append({"action": "REMOVE", "scope": "workspace", "id": value})

    for scope_name in ("projects", "compute_groups", "workspace_specs"):
        prior_map = getattr(previous, scope_name) if previous else {}
        current_map = getattr(current, scope_name)
        keys: tuple[str, ...]
        if scope_name == "projects":
            keys = ("id", "project_id")
        elif scope_name == "compute_groups":
            keys = ("id",)
        else:
            keys = ("spec_id",)
        for workspace_id in sorted(set(prior_map) | set(current_map)):
            prior = prior_map.get(workspace_id, Observation())
            observed = current_map.get(workspace_id, Observation())
            if observed.status == "failed":
                changes.append(
                    {"action": "PRESERVE_STALE", "scope": scope_name, "id": workspace_id}
                )
                continue
            prior_ids = _ids(prior, *keys)
            current_ids = _ids(observed, *keys)
            for value in sorted(current_ids - prior_ids):
                changes.append({"action": "ADD", "scope": scope_name, "id": value})
            for value in sorted(prior_ids - current_ids):
                changes.append({"action": "REMOVE", "scope": scope_name, "id": value})
            prior_by_id = {
                next((str(item.get(key) or "") for key in keys if item.get(key)), ""): dict(item)
                for item in prior.items
            }
            current_by_id = {
                next((str(item.get(key) or "") for key in keys if item.get(key)), ""): dict(item)
                for item in observed.items
            }
            for value in sorted(prior_ids & current_ids):
                if prior_by_id.get(value) != current_by_id.get(value):
                    changes.append({"action": "UPDATE", "scope": scope_name, "id": value})
    return changes


__all__ = [
    "build_project_selectors",
    "build_workspace_selectors",
    "observe_account_discovery",
    "state_diff",
]
