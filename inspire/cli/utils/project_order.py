"""Helpers for project-order resolution and config mutation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from inspire.config import Config, ConfigError, PROJECT_CONFIG_DIR
from inspire.config.toml import _find_project_config
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module


@dataclass(frozen=True)
class ProjectOrderResolution:
    project_id: str
    name: str


def _unique_workspace_ids(values: Iterable[str | None]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        workspace_id = str(value or "").strip()
        if not workspace_id or workspace_id in seen:
            continue
        seen.add(workspace_id)
        unique.append(workspace_id)
    return unique


def fetch_current_account_projects(
    config: Config | None = None,
) -> list[browser_api_module.ProjectInfo]:
    """Fetch projects available to the current authenticated account."""
    config = config or Config.from_files_and_env(require_credentials=False)[0]
    try:
        session = web_session_module.get_web_session()
    except (ValueError, ConfigError) as exc:
        raise ConfigError(str(exc)) from exc

    workspace_ids = getattr(session, "all_workspace_ids", None)
    if workspace_ids is None:
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

        candidates = [str(item.get("id") or "") for item in try_enumerate_workspaces(session)]
        candidates.append(str(getattr(session, "workspace_id", "") or ""))
        workspace_ids = _unique_workspace_ids(candidates)
    else:
        workspace_ids = _unique_workspace_ids(list(workspace_ids))

    projects: list[browser_api_module.ProjectInfo] = []
    seen: set[str] = set()

    def add(items: list[browser_api_module.ProjectInfo]) -> None:
        for project in items:
            if project.project_id in seen:
                continue
            seen.add(project.project_id)
            projects.append(project)

    if workspace_ids:
        errors: list[str] = []
        for workspace_id in workspace_ids:
            try:
                add(
                    browser_api_module.list_projects(
                        workspace_id=workspace_id,
                        session=session,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{workspace_id}: {exc}")
        if projects:
            return projects
        if errors:
            raise ConfigError("Failed to list current-account projects: " + "; ".join(errors[:3]))

    try:
        return browser_api_module.list_projects(session=session)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"Failed to list current-account projects: {exc}") from exc


def resolve_project_ref(
    value: str,
    *,
    projects: list[browser_api_module.ProjectInfo],
) -> ProjectOrderResolution:
    """Resolve a user-entered project ID or current project name to a project ID."""
    normalized = str(value or "").strip()
    if not normalized:
        raise ConfigError("Project cannot be empty.")

    for project in projects:
        if project.project_id == normalized:
            return ProjectOrderResolution(project_id=project.project_id, name=project.name)

    name_matches = [
        project for project in projects if project.name.casefold() == normalized.casefold()
    ]
    if len(name_matches) == 1:
        project = name_matches[0]
        return ProjectOrderResolution(project_id=project.project_id, name=project.name)
    if len(name_matches) > 1:
        ids = ", ".join(project.project_id for project in name_matches)
        raise ConfigError(f"Project name '{value}' is ambiguous; matching project IDs: {ids}")

    valid = ", ".join(
        f"{project.name} ({project.project_id})" for project in projects if project.name
    )
    suffix = f" Available projects: {valid}" if valid else ""
    raise ConfigError(f"Project '{value}' not found for the current account.{suffix}")


def resolve_project_refs(
    values: list[str] | tuple[str, ...],
    *,
    projects: list[browser_api_module.ProjectInfo],
) -> list[str]:
    project_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        resolved = resolve_project_ref(value, projects=projects)
        if resolved.project_id in seen:
            continue
        seen.add(resolved.project_id)
        project_ids.append(resolved.project_id)
    return project_ids


def normalize_project_order(
    project_order: list[str],
    *,
    projects: list[browser_api_module.ProjectInfo],
) -> list[str]:
    """Resolve saved project_order entries to stable IDs and drop stale legacy names."""
    project_ids: list[str] = []
    seen: set[str] = set()
    for entry in project_order:
        try:
            resolved = resolve_project_ref(entry, projects=projects)
        except ConfigError:
            continue
        if resolved.project_id in seen:
            continue
        seen.add(resolved.project_id)
        project_ids.append(resolved.project_id)
    return project_ids


def normalize_project_order_preserving_unresolved(
    project_order: list[str],
    *,
    projects: list[browser_api_module.ProjectInfo],
) -> list[str]:
    """Resolve known entries to IDs, dedupe them, and keep unresolved saved IDs."""
    project_ids: list[str] = []
    seen: set[str] = set()
    for entry in project_order:
        raw_entry = str(entry or "").strip()
        if not raw_entry:
            continue
        try:
            resolved = resolve_project_ref(raw_entry, projects=projects)
            value = resolved.project_id
        except ConfigError:
            if not looks_like_stable_project_id(raw_entry):
                continue
            value = raw_entry
        if value in seen:
            continue
        seen.add(value)
        project_ids.append(value)
    return project_ids


def looks_like_stable_project_id(value: str) -> bool:
    normalized = str(value or "").strip()
    return normalized.startswith(("project-", "p-"))


def project_config_path() -> Path:
    return _find_project_config() or (Path.cwd() / PROJECT_CONFIG_DIR / "config.toml")


def load_toml_doc(path: Path) -> tomlkit.TOMLDocument:
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Inspire CLI Project Configuration"))
    doc.add(tomlkit.nl())
    return doc


def write_project_order(
    project_ids: list[str],
    *,
    path: Path | None = None,
) -> Path:
    path = path or project_config_path()
    doc = load_toml_doc(path)

    if "defaults" not in doc or not isinstance(doc["defaults"], dict):
        doc["defaults"] = tomlkit.table()

    defaults = doc["defaults"]
    if project_ids:
        defaults["project_order"] = project_ids
    else:
        defaults.pop("project_order", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return path


def read_project_order(*, path: Path | None = None) -> list[str]:
    path = path or project_config_path()
    if not path.exists():
        return []
    doc = load_toml_doc(path)
    defaults = doc.get("defaults")
    if not isinstance(defaults, dict):
        return []
    raw_order = defaults.get("project_order")
    if not isinstance(raw_order, list):
        return []
    return [str(item) for item in raw_order if str(item or "").strip()]


def describe_project_order(
    project_order: list[str],
    *,
    projects: list[browser_api_module.ProjectInfo],
    source: str | None = None,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    projects_by_id = {project.project_id: project for project in projects}
    resolved: list[dict[str, str]] = []
    unresolved: list[str] = []
    for project_id in project_order:
        project = projects_by_id.get(project_id)
        if project is None:
            unresolved.append(project_id)
            continue
        resolved.append({"project_id": project.project_id, "name": project.name})
    payload: dict[str, Any] = {
        "project_order": list(project_order),
        "resolved": resolved,
        "unresolved": unresolved,
    }
    if source is not None:
        payload["source"] = source
    if config_path is not None:
        payload["config_path"] = str(config_path)
    return payload
