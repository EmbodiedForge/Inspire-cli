"""Live project selector resolution."""

from __future__ import annotations

from typing import Any, Iterable

from inspire.config import Config, ConfigError
from inspire.platform.web.session import WebSession


def resolve_project_request(
    config: Config,
    requested: str | None,
    *,
    workspace_id: str,
    projects: Iterable[Any],
    session: WebSession,
) -> str | None:
    """Resolve an explicit ID, account selector alias, or exact live project name."""
    value = str(requested or "").strip()
    if not value or value.startswith(("project-", "proj-")):
        return value or None

    selector_alias = next(
        (alias for alias in (config.project_selectors or {}) if alias.lower() == value.lower()),
        None,
    )
    if selector_alias:
        selector = config.project_selectors[selector_alias]
        workspace_alias = str(selector.get("workspace") or "").strip()
        name = str(selector.get("name") or "").strip()
        if not workspace_alias or not name:
            raise ConfigError(f"Project alias {selector_alias!r} has an incomplete selector")
        matches = [item for item in projects if str(getattr(item, "name", "")) == name]
        if len(matches) == 1:
            return str(getattr(matches[0], "project_id", ""))
        if len(matches) > 1:
            ids = ", ".join(sorted(str(getattr(item, "project_id", "")) for item in matches))
            raise ConfigError(f"Project alias {selector_alias!r} is ambiguous: {ids}")
        raise ConfigError(
            f"Project alias {selector_alias!r} selects name {name!r}, which is not visible "
            f"in workspace {workspace_id}"
        )

    legacy_alias = next(
        (alias for alias in (config.projects or {}) if alias.lower() == value.lower()),
        None,
    )
    if legacy_alias:
        return str(config.projects[legacy_alias])

    matches = [item for item in projects if str(getattr(item, "name", "")) == value]
    if len(matches) == 1:
        return str(getattr(matches[0], "project_id", ""))
    if len(matches) > 1:
        ids = ", ".join(sorted(str(getattr(item, "project_id", "")) for item in matches))
        raise ConfigError(f"Project name {value!r} is ambiguous in workspace {workspace_id}: {ids}")
    raise ConfigError(f"Project name {value!r} is not visible in workspace {workspace_id}")


__all__ = ["resolve_project_request"]
