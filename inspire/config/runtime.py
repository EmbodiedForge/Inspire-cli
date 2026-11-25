"""White-box remote runtime resolution for bridge and job commands."""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from typing import Any, Sequence

from inspire.config.env import resolve_remote_env
from inspire.config.models import Config, ConfigError
from inspire.config.runtime_state import RuntimeSessionState

_RUNTIME_CWD_HINT = "Set [remote_runtime].cwd or [defaults].target_dir."


@dataclass(frozen=True)
class EffectiveRemoteRuntime:
    cwd: str | None
    cwd_source: str
    path_prepend: tuple[str, ...]
    path_prepend_sources: tuple[str, ...]
    session_path_remove: tuple[str, ...]
    venv_mode: str
    venv_source: str
    venv_path: str | None
    env: dict[str, str]
    env_sources: dict[str, str]
    session_env_unset: tuple[str, ...]


def _normalize_text_list(values: Sequence[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw_value in values or ():
        value = str(raw_value or "").strip()
        if value:
            normalized.append(value)
    return normalized


def _dedupe_ordered_pairs(values: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, source in values:
        if value in seen:
            continue
        deduped.append((value, source))
        seen.add(value)
    return tuple(deduped)


def _join_remote_path(base: str, relative: str) -> str:
    if base == "~":
        prefix = "~"
        tail = ""
    elif base.startswith("~/"):
        prefix = "~"
        tail = base[2:]
    else:
        return posixpath.normpath(posixpath.join(base, relative))

    joined = posixpath.normpath(posixpath.join("/" + tail, relative)).lstrip("/")
    return f"~/{joined}" if joined else prefix


def _resolve_remote_path(path: str, *, cwd: str | None, field_name: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise ConfigError(f"{field_name} cannot be empty.")
    if value == "~" or value.startswith("~/") or value.startswith("/"):
        return value
    if cwd is None:
        raise ConfigError(f"{field_name} requires a cwd for relative paths. {_RUNTIME_CWD_HINT}")
    return _join_remote_path(cwd, value)


def _remote_path_shell_segment(path: str) -> str:
    value = str(path or "").strip()
    if value == "~":
        return '"${HOME}"'
    if value.startswith("~/"):
        suffix = value[2:]
        escaped_suffix = (
            suffix.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        )
        return f'"${{HOME}}/{escaped_suffix}"'
    return shlex.quote(value)


def resolve_effective_remote_runtime(
    config: Config,
    *,
    cli_path_prepend: Sequence[str] | None = None,
    session_state: RuntimeSessionState | None = None,
    require_cwd: bool = False,
) -> EffectiveRemoteRuntime:
    """Resolve the effective remote runtime for bridge and job commands."""
    session_state = session_state or RuntimeSessionState()

    session_cwd = str(session_state.cwd or "").strip() or None
    cwd: str | None
    if session_cwd:
        cwd = session_cwd
        cwd_source = "runtime_session.cwd"
    else:
        cwd = str(config.remote_runtime_cwd or "").strip() or None
        if cwd:
            cwd_source = "remote_runtime.cwd"
        else:
            cwd = str(config.target_dir or "").strip() or None
            cwd_source = "defaults.target_dir" if cwd else "unset"

    if require_cwd and cwd is None:
        raise ConfigError(f"Missing remote runtime cwd. {_RUNTIME_CWD_HINT}")

    cli_paths = _normalize_text_list(cli_path_prepend)
    config_paths = _normalize_text_list(getattr(config, "remote_runtime_path_prepend", []))
    session_add_paths = _normalize_text_list(session_state.path_prepend)
    session_remove_paths = _normalize_text_list(session_state.path_remove)

    resolved_cli_paths = [
        _resolve_remote_path(path, cwd=cwd, field_name="CLI --path-prepend") for path in cli_paths
    ]
    resolved_session_add_paths = [
        _resolve_remote_path(path, cwd=cwd, field_name="runtime session path")
        for path in session_add_paths
    ]
    resolved_session_remove_paths = tuple(
        _resolve_remote_path(path, cwd=cwd, field_name="runtime session path remove")
        for path in session_remove_paths
    )
    resolved_config_paths = [
        _resolve_remote_path(path, cwd=cwd, field_name="remote_runtime.path_prepend")
        for path in config_paths
    ]

    filtered_config_pairs = [
        (path, "remote_runtime.path_prepend")
        for path in resolved_config_paths
        if path not in resolved_session_remove_paths
    ]
    path_pairs = _dedupe_ordered_pairs(
        [(path, "cli --path-prepend") for path in resolved_cli_paths]
        + [(path, "runtime_session.path_prepend") for path in resolved_session_add_paths]
        + filtered_config_pairs
    )
    resolved_paths = tuple(path for path, _source in path_pairs)
    path_sources = tuple(source for _path, source in path_pairs)

    legacy_env = resolve_remote_env(getattr(config, "remote_env", {}) or {})
    runtime_env = resolve_remote_env(getattr(config, "remote_runtime_env", {}) or {})
    merged_env = dict(legacy_env)
    merged_env.update(runtime_env)
    merged_sources = {key: "remote_env" for key in legacy_env}
    merged_sources.update({key: "remote_runtime.env" for key in runtime_env})
    normalized_env_unset = tuple(_normalize_text_list(session_state.env_unset))
    for key in normalized_env_unset:
        merged_env.pop(key, None)
        merged_sources.pop(key, None)

    session_env = resolve_remote_env(session_state.env)
    merged_env.update(session_env)
    merged_sources.update({key: "runtime_session.env" for key in session_env})

    session_venv = str(session_state.venv or "").strip()
    raw_venv = (
        session_venv
        or str(getattr(config, "remote_runtime_venv", "auto") or "auto").strip()
        or "auto"
    )
    lowered_venv = raw_venv.lower()
    if lowered_venv == "off":
        venv_mode = "off"
        if session_venv:
            venv_source = "runtime_session.venv"
        else:
            venv_source = "remote_runtime.venv" if raw_venv != "auto" else "default"
        venv_path = None
    elif lowered_venv == "auto":
        venv_mode = "auto"
        if session_venv:
            venv_source = "runtime_session.venv"
        else:
            venv_source = "remote_runtime.venv" if raw_venv != "auto" else "default"
        venv_path = _join_remote_path(cwd, ".venv") if cwd is not None else None
    else:
        venv_mode = "path"
        venv_source = "runtime_session.venv" if session_venv else "remote_runtime.venv"
        venv_path = _resolve_remote_path(raw_venv, cwd=cwd, field_name="remote_runtime.venv")

    return EffectiveRemoteRuntime(
        cwd=cwd,
        cwd_source=cwd_source,
        path_prepend=resolved_paths,
        path_prepend_sources=path_sources,
        session_path_remove=resolved_session_remove_paths,
        venv_mode=venv_mode,
        venv_source=venv_source,
        venv_path=venv_path,
        env=merged_env,
        env_sources=merged_sources,
        session_env_unset=normalized_env_unset,
    )


def build_runtime_export_commands(runtime: EffectiveRemoteRuntime) -> list[str]:
    """Build ordered export commands for the effective runtime."""
    commands = [f"export {key}={shlex.quote(value)}" for key, value in runtime.env.items()]
    if runtime.path_prepend:
        path_expr = ":".join(_remote_path_shell_segment(path) for path in runtime.path_prepend)
        commands.append(f'export PATH={path_expr}:"$PATH"')
    return commands


def _remote_error_print_command(message: str) -> str:
    """Emit an error message on stderr without letting it re-enter shell quoting."""
    return f"printf '%s\\n' {shlex.quote(message)} >&2"


def build_runtime_activate_command(runtime: EffectiveRemoteRuntime) -> str | None:
    """Build the venv activation command for the effective runtime."""
    if runtime.venv_mode == "off" or runtime.venv_path is None:
        return None

    activate_path = f"{runtime.venv_path}/bin/activate"
    quoted_activate = _remote_path_shell_segment(activate_path)
    if runtime.venv_mode == "auto":
        return f"[ -f {quoted_activate} ] && source {quoted_activate} || true"

    not_found = _remote_error_print_command(f"venv activate script not found: {activate_path}")
    return (
        f"if [ -f {quoted_activate} ]; then source {quoted_activate}; else {not_found}; exit 1; fi"
    )


def build_runtime_cd_command(runtime: EffectiveRemoteRuntime) -> str | None:
    """Build the working-directory change command for the effective runtime."""
    if runtime.cwd is None:
        return None

    quoted_cwd = _remote_path_shell_segment(runtime.cwd)
    not_found = _remote_error_print_command(f"remote runtime cwd not found: {runtime.cwd}")
    hint = _remote_error_print_command(f"  {_RUNTIME_CWD_HINT}")
    return f"cd {quoted_cwd} || {{ {not_found}; {hint}; exit 1; }}"


def build_runtime_layers(
    runtime: EffectiveRemoteRuntime,
    *,
    user_command: str | None,
    include_python_unbuffered: bool,
) -> list[dict[str, Any]]:
    """Build ordered explain layers for the effective runtime."""
    layers: list[dict[str, Any]] = []

    for key in runtime.session_env_unset:
        layers.append(
            {
                "kind": "env_unset",
                "key": key,
                "source": "runtime_session.env_unset",
            }
        )

    for key, value in runtime.env.items():
        layers.append(
            {
                "kind": "env_export",
                "key": key,
                "value": value,
                "source": runtime.env_sources.get(key, "remote_env"),
            }
        )

    for path in runtime.session_path_remove:
        layers.append(
            {
                "kind": "path_remove",
                "value": path,
                "source": "runtime_session.path_remove",
            }
        )

    for path, source in zip(runtime.path_prepend, runtime.path_prepend_sources, strict=True):
        layers.append(
            {
                "kind": "path_prepend",
                "value": path,
                "source": source,
            }
        )

    if include_python_unbuffered:
        layers.append(
            {
                "kind": "env_export",
                "key": "PYTHONUNBUFFERED",
                "value": "1",
                "source": "bridge_exec_builtin",
            }
        )

    if runtime.venv_mode != "off":
        layers.append(
            {
                "kind": "venv_activate",
                "mode": runtime.venv_mode,
                "value": runtime.venv_path,
                "source": runtime.venv_source,
            }
        )

    if runtime.cwd is not None:
        layers.append(
            {
                "kind": "cd",
                "value": runtime.cwd,
                "source": runtime.cwd_source,
            }
        )

    if user_command is not None:
        layers.append({"kind": "user_command", "value": user_command, "source": "user"})
    return layers


__all__ = [
    "EffectiveRemoteRuntime",
    "build_runtime_activate_command",
    "build_runtime_cd_command",
    "build_runtime_export_commands",
    "build_runtime_layers",
    "resolve_effective_remote_runtime",
]
