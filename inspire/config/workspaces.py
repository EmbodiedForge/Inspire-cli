"""Explicit workspace ID, display-name, and user-alias resolution."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from inspire.config import Config, ConfigError

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession

_WORKSPACE_ID_RE = re.compile(
    r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PLACEHOLDER_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"

WORKSPACE_ALIAS_HELP = (
    "Workspace ID, exact platform display name, or user-configured alias from "
    '[accounts."<alias>".workspaces]. Aliases contain only {name = "..."}; '
    "resource class and internet reachability are not implied. See 'inspire workspace list'."
)


def _validate_workspace_id(value: str) -> None:
    if value == _PLACEHOLDER_WORKSPACE_ID:
        raise ConfigError(
            "workspace_id is set to the placeholder value. "
            "Configure a real workspace id or pass --workspace-id."
        )
    if not _WORKSPACE_ID_RE.match(value):
        raise ConfigError(f"Invalid workspace_id format: {value!r}")


def _workspace_rows(config: Config, session: WebSession | None) -> list[dict[str, Any]]:
    if session is None:
        from inspire.platform.web.session import get_web_session

        session = get_web_session(account=config.account_alias)

    from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

    rows = try_enumerate_workspaces(
        session,
        workspace_id=str(session.workspace_id or ""),
        base_url=str(session.base_url or config.base_url or ""),
    )
    if not rows:
        raise ConfigError(
            "The workspace list could not be fetched for account "
            f"{config.account_alias or '<none>'!r}."
        )
    return rows


def _matching_workspace_ids(rows: list[dict[str, Any]], name: str) -> list[str]:
    return sorted(
        {
            str(item.get("id") or "").strip()
            for item in rows
            if isinstance(item, dict) and str(item.get("name") or "").strip() == name
        }
        - {""}
    )


def _visible_workspace_names(rows: list[dict[str, Any]]) -> str:
    visible = sorted(
        {
            str(item.get("name") or "").strip()
            for item in rows
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
    )
    return ", ".join(visible) or "(none)"


def resolve_workspace_aliases(
    config: Config,
    *,
    session: WebSession | None = None,
) -> dict[str, str]:
    """Resolve every user-written workspace alias against one live workspace list."""
    selectors = config.workspace_selectors or {}
    if not selectors:
        return {}
    rows = _workspace_rows(config, session)
    resolved: dict[str, str] = {}
    for alias, selector in selectors.items():
        name = str((selector or {}).get("name") or "").strip()
        if not name:
            raise ConfigError(
                f"Workspace alias {alias!r} has no name selector; set "
                f'[accounts."{config.account_alias or "<alias>"}".workspaces.{alias}].name.'
            )
        matches = _matching_workspace_ids(rows, name)
        if len(matches) != 1:
            reason = "is ambiguous" if matches else "is not visible"
            raise ConfigError(f"Workspace alias {alias!r} selects name {name!r}, which {reason}")
        _validate_workspace_id(matches[0])
        resolved[alias] = matches[0]
    return resolved


def select_workspace_id(
    config: Config,
    *,
    explicit_workspace_id: str | None = None,
    explicit_workspace_name: str | None = None,
    session: WebSession | None = None,
) -> str | None:
    """Resolve an explicit workspace ID, exact platform name, or user alias."""
    if explicit_workspace_id:
        _validate_workspace_id(explicit_workspace_id)
        return explicit_workspace_id

    key = str(explicit_workspace_name or "").strip()
    if not key:
        return None
    if _WORKSPACE_ID_RE.match(key):
        _validate_workspace_id(key)
        return key

    selectors = config.workspace_selectors or {}
    alias = next((name for name in selectors if name.lower() == key.lower()), None)
    selected_name = str((selectors.get(alias) or {}).get("name") or "").strip() if alias else key
    if alias and not selected_name:
        raise ConfigError(
            f"Workspace alias {alias!r} has no name selector; set "
            f'[accounts."{config.account_alias or "<alias>"}".workspaces.{alias}].name.'
        )

    rows = _workspace_rows(config, session)
    matches = _matching_workspace_ids(rows, selected_name)
    if len(matches) > 1:
        raise ConfigError(
            f"Workspace name {selected_name!r} is ambiguous; matching IDs: {', '.join(matches)}"
        )
    if not matches:
        subject = f"alias {alias!r} selecting {selected_name!r}" if alias else repr(key)
        raise ConfigError(
            f"Workspace {subject} is not visible. Visible workspaces: "
            f"{_visible_workspace_names(rows)}. Use an exact display name or ID."
        )
    _validate_workspace_id(matches[0])
    return matches[0]
