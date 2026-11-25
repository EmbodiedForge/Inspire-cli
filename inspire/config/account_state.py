"""Account-scoped, rebuildable discovery state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from inspire.config.toml_write import atomic_write_text

STATE_VERSION = 1
_STATE_DIR_ENV = "INSPIRE_STATE_DIR"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_alias(alias: str) -> str:
    raw = str(alias or "").strip()
    if not raw:
        raise ValueError("Account alias cannot be empty")
    if raw not in {".", ".."} and re.fullmatch(r"[A-Za-z0-9._-]+", raw):
        return raw
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-") or "account"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{slug[:48]}-{digest}"


def resolve_account_state_root() -> Path:
    override = str(os.getenv(_STATE_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    xdg_state_home = str(os.getenv("XDG_STATE_HOME") or "").strip()
    base = Path(xdg_state_home).expanduser() if xdg_state_home else Path.home() / ".local/state"
    return base / "inspire-cli"


def get_account_discovery_state_path(alias: str) -> Path:
    return resolve_account_state_root() / "accounts" / _safe_alias(alias) / "discovery.json"


@dataclass(frozen=True)
class Observation:
    """One platform observation, including preserved data after a failed refresh."""

    status: str = "missing"
    observed_at: str | None = None
    last_success_at: str | None = None
    items: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    @property
    def stale(self) -> bool:
        return self.status == "failed" and bool(self.items)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "observed_at": self.observed_at,
            "last_success_at": self.last_success_at,
            "items": [dict(item) for item in self.items],
        }
        if self.error:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "Observation":
        if not isinstance(payload, Mapping):
            return cls()
        raw_items = payload.get("items")
        items = tuple(dict(item) for item in raw_items or [] if isinstance(item, Mapping))
        return cls(
            status=str(payload.get("status") or "missing"),
            observed_at=str(payload.get("observed_at") or "").strip() or None,
            last_success_at=str(payload.get("last_success_at") or "").strip() or None,
            items=items,
            error=str(payload.get("error") or "").strip() or None,
        )

    @classmethod
    def success(
        cls,
        items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        observed_at: str | None = None,
    ) -> "Observation":
        timestamp = observed_at or utc_now_iso()
        return cls(
            status="success",
            observed_at=timestamp,
            last_success_at=timestamp,
            items=tuple(dict(item) for item in items),
        )

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        observed_at: str | None = None,
    ) -> "Observation":
        return cls(
            status="failed",
            observed_at=observed_at or utc_now_iso(),
            error=str(error or "Unknown discovery error"),
        )


def reconcile_observation(previous: Observation, current: Observation) -> Observation:
    """Replace successful scopes and preserve last-known data for failed scopes."""
    if current.succeeded:
        return current
    if current.status != "failed":
        return previous
    return Observation(
        status="failed",
        observed_at=current.observed_at,
        last_success_at=previous.last_success_at,
        items=previous.items,
        error=current.error,
    )


@dataclass
class AccountDiscoveryState:
    """Versioned discovery snapshot for one explicit account alias."""

    account_alias: str
    platform_username: str
    base_url: str
    updated_at: str = field(default_factory=utc_now_iso)
    status: str = "complete"
    workspaces: Observation = field(default_factory=Observation)
    projects: dict[str, Observation] = field(default_factory=dict)
    compute_groups: dict[str, Observation] = field(default_factory=dict)
    workspace_specs: dict[str, Observation] = field(default_factory=dict)
    workspace_aliases: dict[str, dict[str, Any]] = field(default_factory=dict)
    project_aliases: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = STATE_VERSION

    def observations(self) -> list[Observation]:
        return [
            self.workspaces,
            *self.projects.values(),
            *self.compute_groups.values(),
            *self.workspace_specs.values(),
        ]

    def refresh_status(self) -> None:
        self.status = (
            "partial"
            if any(item.status in {"failed", "missing"} for item in self.observations())
            else "complete"
        )
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "account_alias": self.account_alias,
            "platform_username": self.platform_username,
            "base_url": self.base_url,
            "updated_at": self.updated_at,
            "status": self.status,
            "workspaces": self.workspaces.to_dict(),
            "projects": {key: value.to_dict() for key, value in sorted(self.projects.items())},
            "compute_groups": {
                key: value.to_dict() for key, value in sorted(self.compute_groups.items())
            },
            "workspace_specs": {
                key: value.to_dict() for key, value in sorted(self.workspace_specs.items())
            },
            "resolutions": {
                "workspace_aliases": self.workspace_aliases,
                "project_aliases": self.project_aliases,
            },
        }

    @classmethod
    def from_dict(cls, payload: object) -> "AccountDiscoveryState | None":
        if not isinstance(payload, Mapping):
            return None
        if int(payload.get("version") or 0) != STATE_VERSION:
            return None
        alias = str(payload.get("account_alias") or "").strip()
        if not alias:
            return None
        resolutions = payload.get("resolutions")
        if not isinstance(resolutions, Mapping):
            resolutions = {}

        def _observations(key: str) -> dict[str, Observation]:
            raw = payload.get(key)
            if not isinstance(raw, Mapping):
                return {}
            return {str(item_key): Observation.from_dict(value) for item_key, value in raw.items()}

        state = cls(
            version=STATE_VERSION,
            account_alias=alias,
            platform_username=str(payload.get("platform_username") or ""),
            base_url=str(payload.get("base_url") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            status=str(payload.get("status") or "complete"),
            workspaces=Observation.from_dict(payload.get("workspaces")),
            projects=_observations("projects"),
            compute_groups=_observations("compute_groups"),
            workspace_specs=_observations("workspace_specs"),
            workspace_aliases=dict(resolutions.get("workspace_aliases") or {}),
            project_aliases=dict(resolutions.get("project_aliases") or {}),
        )
        return state

    def workspace_items(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.workspaces.items]

    def workspace_names(self) -> dict[str, str]:
        return {
            str(item.get("id") or ""): str(item.get("name") or "")
            for item in self.workspaces.items
            if str(item.get("id") or "").strip()
        }

    def project_catalog(self) -> dict[str, dict[str, Any]]:
        catalog: dict[str, dict[str, Any]] = {}
        for workspace_id, observation in self.projects.items():
            for item in observation.items:
                project_id = str(item.get("id") or item.get("project_id") or "").strip()
                if not project_id:
                    continue
                entry = catalog.setdefault(project_id, {})
                entry.update(dict(item))
                workspace_ids = set(entry.get("workspace_ids") or [])
                workspace_ids.add(workspace_id)
                entry["workspace_ids"] = sorted(workspace_ids)
        return catalog

    def flattened_compute_groups(self) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for workspace_id, observation in self.compute_groups.items():
            for item in observation.items:
                group_id = str(item.get("id") or item.get("logic_compute_group_id") or "").strip()
                if not group_id:
                    continue
                entry = groups.setdefault(group_id, {})
                entry.update(dict(item))
                workspace_ids = set(entry.get("workspace_ids") or [])
                workspace_ids.add(workspace_id)
                entry["workspace_ids"] = sorted(workspace_ids)
        return sorted(groups.values(), key=lambda item: str(item.get("id") or ""))

    def flattened_workspace_specs(self) -> dict[str, list[dict[str, Any]]]:
        return {
            workspace_id: [dict(item) for item in observation.items]
            for workspace_id, observation in self.workspace_specs.items()
            if observation.items
        }


def load_account_discovery_state(alias: str) -> AccountDiscoveryState | None:
    path = get_account_discovery_state_path(alias)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    state = AccountDiscoveryState.from_dict(payload)
    if state is None or state.account_alias != str(alias or "").strip():
        return None
    return state


def save_account_discovery_state(state: AccountDiscoveryState) -> Path:
    state.refresh_status()
    path = get_account_discovery_state_path(state.account_alias)
    atomic_write_text(
        path,
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return path


def clear_account_discovery_state(alias: str) -> None:
    path = get_account_discovery_state_path(alias)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    try:
        path.parent.rmdir()
    except OSError:
        pass


def reconcile_account_discovery_state(
    previous: AccountDiscoveryState | None,
    current: AccountDiscoveryState,
) -> AccountDiscoveryState:
    if previous is None:
        current.refresh_status()
        return current

    current.workspaces = reconcile_observation(previous.workspaces, current.workspaces)
    live_workspace_ids = {
        str(item.get("id") or "").strip() for item in current.workspaces.items if item.get("id")
    }
    for field_name in ("projects", "compute_groups", "workspace_specs"):
        prior_map: dict[str, Observation] = getattr(previous, field_name)
        current_map: dict[str, Observation] = getattr(current, field_name)
        reconciled: dict[str, Observation] = {}
        for workspace_id in sorted(set(prior_map) | set(current_map)):
            if current.workspaces.succeeded and workspace_id not in live_workspace_ids:
                continue
            reconciled[workspace_id] = reconcile_observation(
                prior_map.get(workspace_id, Observation()),
                current_map.get(workspace_id, Observation()),
            )
        setattr(current, field_name, reconciled)

    # Selector builders are authoritative for resolutions. Retaining old
    # resolution rows after a successful scope refresh would resurrect deleted
    # aliases and make last-seen IDs look like durable intent.
    current.refresh_status()
    return current


__all__ = [
    "AccountDiscoveryState",
    "Observation",
    "STATE_VERSION",
    "clear_account_discovery_state",
    "get_account_discovery_state_path",
    "load_account_discovery_state",
    "reconcile_account_discovery_state",
    "reconcile_observation",
    "resolve_account_state_root",
    "save_account_discovery_state",
    "utc_now_iso",
]
