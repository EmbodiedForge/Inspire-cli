"""Session-scoped runtime state for agent-friendly execution overrides."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspire.config.models import Config
from inspire.config.toml_write import atomic_write_text

_CACHE_BASENAME = "runtime-session"
_CACHE_VERSION = 1


def _default_cache_dir() -> Path:
    return Path.home() / ".cache" / "inspire-cli"


def _normalize_text_list(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values or ():
        value = str(raw_value or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _normalize_env_dict(values: dict[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in (values or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        normalized[key] = str(raw_value)
    return normalized


@dataclass(frozen=True)
class RuntimeSessionScope:
    """Identity and storage location for a session runtime state file."""

    identity: str
    key: str
    state_file: Path
    project_path: str | None
    local_cwd: str


@dataclass
class RuntimeSessionState:
    """Patch-style runtime overrides layered above config/env defaults."""

    cwd: str | None = None
    venv: str | None = None
    path_prepend: tuple[str, ...] = field(default_factory=tuple)
    path_remove: tuple[str, ...] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)
    env_unset: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.path_prepend = _normalize_text_list(list(self.path_prepend))
        self.path_remove = _normalize_text_list(list(self.path_remove))
        self.env = _normalize_env_dict(self.env)
        self.env_unset = _normalize_text_list(list(self.env_unset))

    def is_empty(self) -> bool:
        return not any(
            (
                self.cwd,
                self.venv,
                self.path_prepend,
                self.path_remove,
                self.env,
                self.env_unset,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "venv": self.venv,
            "path_prepend": list(self.path_prepend),
            "path_remove": list(self.path_remove),
            "env": dict(self.env),
            "env_unset": list(self.env_unset),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RuntimeSessionState":
        payload = payload or {}
        raw_path_prepend = payload.get("path_prepend")
        raw_path_remove = payload.get("path_remove")
        raw_env = payload.get("env")
        raw_env_unset = payload.get("env_unset")
        return cls(
            cwd=str(payload.get("cwd") or "").strip() or None,
            venv=str(payload.get("venv") or "").strip() or None,
            path_prepend=tuple(raw_path_prepend) if isinstance(raw_path_prepend, list) else (),
            path_remove=tuple(raw_path_remove) if isinstance(raw_path_remove, list) else (),
            env=raw_env if isinstance(raw_env, dict) else {},
            env_unset=tuple(raw_env_unset) if isinstance(raw_env_unset, list) else (),
        )


def resolve_runtime_session_scope(
    *,
    config: Config | None = None,
    cwd: Path | None = None,
) -> RuntimeSessionScope:
    """Resolve the account-and-project-scoped session identity and cache path."""
    local_cwd_path = (cwd or Path.cwd()).resolve()
    project_config_path = getattr(config, "_project_config_path", None)
    project_path: str | None = None
    if project_config_path is not None:
        project_path = str(Path(project_config_path).resolve())
        legacy_identity = f"project:{project_path}"
    else:
        legacy_identity = f"cwd:{local_cwd_path}"

    account_key = config.get_account_context().cache_key if config is not None else ""
    identity = f"{legacy_identity}:account:{account_key}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    state_file = _default_cache_dir() / f"{_CACHE_BASENAME}-{key}.json"
    return RuntimeSessionScope(
        identity=identity,
        key=key,
        state_file=state_file,
        project_path=project_path,
        local_cwd=str(local_cwd_path),
    )


def load_runtime_session_state_for_config(config: Config) -> RuntimeSessionState:
    """Load session state only for configs produced by layered config loading."""
    if not hasattr(config, "_project_config_path"):
        return RuntimeSessionState()
    return load_runtime_session_state(resolve_runtime_session_scope(config=config))


def load_runtime_session_state(scope: RuntimeSessionScope) -> RuntimeSessionState:
    """Load session runtime state for the given scope."""
    path = scope.state_file
    if not path.exists():
        return RuntimeSessionState()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return RuntimeSessionState()

    if not isinstance(raw, dict):
        return RuntimeSessionState()

    runtime_payload = raw.get("runtime")
    if runtime_payload is None:
        runtime_payload = raw
    if not isinstance(runtime_payload, dict):
        return RuntimeSessionState()
    return RuntimeSessionState.from_dict(runtime_payload)


def save_runtime_session_state(scope: RuntimeSessionScope, state: RuntimeSessionState) -> None:
    """Persist session runtime state, removing the file when empty."""
    if state.is_empty():
        clear_runtime_session_state(scope)
        return

    payload = {
        "version": _CACHE_VERSION,
        "identity": scope.identity,
        "key": scope.key,
        "project_path": scope.project_path,
        "local_cwd": scope.local_cwd,
        "runtime": state.to_dict(),
    }
    path = scope.state_file
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def clear_runtime_session_state(scope: RuntimeSessionScope) -> None:
    """Remove any persisted session runtime state for the scope."""
    try:
        scope.state_file.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


__all__ = [
    "RuntimeSessionScope",
    "RuntimeSessionState",
    "clear_runtime_session_state",
    "load_runtime_session_state",
    "load_runtime_session_state_for_config",
    "resolve_runtime_session_scope",
    "save_runtime_session_state",
]
