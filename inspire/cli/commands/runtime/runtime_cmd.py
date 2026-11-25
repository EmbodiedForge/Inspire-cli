"""Runtime state commands."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import click
import tomlkit

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.logging_setup import redact_env_value
from inspire.cli.utils import project_order as project_order_utils
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_success
from inspire.config import (
    PROJECT_CONFIG_DIR,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_INFERRED,
    SOURCE_PROJECT,
    Config,
    ConfigError,
    RuntimeSessionState,
    build_runtime_layers,
    clear_runtime_session_state,
    load_runtime_session_state,
    resolve_effective_remote_runtime,
    resolve_runtime_session_scope,
    save_runtime_session_state,
)
from inspire.config.toml import _find_project_config, _load_toml

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_text_list(values: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for raw_value in values:
        value = str(raw_value or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _require_non_empty(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ConfigError(f"{field_name} cannot be empty.")
    return normalized


def _require_env_key(key: str) -> str:
    normalized = str(key or "").strip()
    if not _ENV_KEY_RE.match(normalized):
        raise ConfigError(f"Invalid env key: {key!r}")
    return normalized


def _resolve_project_config_path() -> Path:
    return _find_project_config() or (Path.cwd() / PROJECT_CONFIG_DIR / "config.toml")


def _load_toml_doc(path: Path) -> tomlkit.TOMLDocument:
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.document()


def _save_toml_doc(path: Path, doc: tomlkit.TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _ensure_runtime_table(doc: tomlkit.TOMLDocument) -> Any:
    if "remote_runtime" not in doc or not isinstance(doc["remote_runtime"], dict):
        doc["remote_runtime"] = tomlkit.table()
    return doc["remote_runtime"]


def _cleanup_runtime_table(doc: tomlkit.TOMLDocument) -> None:
    runtime_table = doc.get("remote_runtime")
    if not isinstance(runtime_table, dict):
        return
    env_table = runtime_table.get("env")
    if isinstance(env_table, dict) and not env_table:
        del runtime_table["env"]
    if not runtime_table:
        del doc["remote_runtime"]


def _load_project_runtime_raw(project_config_path: Path | None) -> dict[str, Any]:
    if project_config_path is None or not project_config_path.exists():
        return {}
    raw_data = _load_toml(project_config_path)
    remote_runtime = raw_data.get("remote_runtime")
    if not isinstance(remote_runtime, dict):
        return {}

    result: dict[str, Any] = {}
    cwd = str(remote_runtime.get("cwd") or "").strip()
    if cwd:
        result["cwd"] = cwd
    raw_paths = remote_runtime.get("path_prepend")
    if isinstance(raw_paths, list):
        result["path_prepend"] = [str(item) for item in raw_paths if str(item or "").strip()]
    venv = str(remote_runtime.get("venv") or "").strip()
    if venv:
        result["venv"] = venv
    env_table = remote_runtime.get("env")
    if isinstance(env_table, dict):
        result["env"] = {
            str(key): str(value) for key, value in env_table.items() if str(key or "").strip()
        }
    return result


def _runtime_to_dict(runtime: Any) -> dict[str, Any]:
    return {
        "cwd": runtime.cwd,
        "cwd_source": runtime.cwd_source,
        "path_prepend": [
            {"value": path, "source": source}
            for path, source in zip(runtime.path_prepend, runtime.path_prepend_sources, strict=True)
        ],
        "venv": {
            "mode": runtime.venv_mode,
            "source": runtime.venv_source,
            "path": runtime.venv_path,
        },
        "env": [
            {
                "key": key,
                "value": redact_env_value(key, value),
                "source": runtime.env_sources.get(key, "remote_env"),
            }
            for key, value in runtime.env.items()
        ],
    }


def _project_order_source_details(config: Config) -> tuple[str | None, Path | None]:
    sources = getattr(config, "_sources", {}) or {}
    source = sources.get("project_order")
    if source == SOURCE_PROJECT:
        return source, getattr(config, "_project_config_path", None)
    if source == SOURCE_GLOBAL:
        return source, getattr(config, "_global_config_path", None)
    if source in {SOURCE_ENV, SOURCE_DEFAULT, SOURCE_INFERRED}:
        return source, None
    return None, None


def _effective_project_order_layer() -> tuple[list[str], str | None, str | None]:
    """Re-resolve the effective project order and the layer providing it."""
    config, _sources = Config.from_files_and_env(
        require_credentials=False, require_target_dir=False
    )
    source, config_path = _project_order_source_details(config)
    effective = [str(entry) for entry in (getattr(config, "project_order", None) or [])]
    return effective, source, (str(config_path) if config_path else None)


def _project_order_layer_label(source: str | None, config_path: str | None) -> str:
    if source == SOURCE_ENV:
        return "environment variable INSPIRE_PROJECT_ORDER"
    if config_path:
        return f"{source or 'another'} config ({config_path})"
    return f"{source or 'another'} configuration layer"


def _project_order_not_cleared_warning(
    *, action: str, source: str | None, config_path: str | None
) -> str:
    return (
        f"{action} only changed the project config; project_order is still effective from "
        f"{_project_order_layer_label(source, config_path)}."
    )


def _build_project_selection_payload(config: Config) -> dict[str, Any]:
    project_order = list(getattr(config, "project_order", []) or [])
    projects = []
    resolution_error = None
    if project_order:
        try:
            projects = project_order_utils.fetch_current_account_projects(config)
        except Exception as exc:  # noqa: BLE001
            resolution_error = str(exc)

    source, config_path = _project_order_source_details(config)
    payload = project_order_utils.describe_project_order(
        project_order,
        projects=projects,
        source=source,
        config_path=config_path,
    )
    if resolution_error:
        payload["resolution_error"] = resolution_error
        payload["unresolved"] = list(project_order)
    return payload


def _build_runtime_payload(
    *,
    config: Config,
    session_state: RuntimeSessionState,
    include_layers: bool,
) -> dict[str, Any]:
    scope = resolve_runtime_session_scope(config=config)
    runtime = resolve_effective_remote_runtime(
        config,
        require_cwd=False,
        session_state=session_state,
    )
    session_payload = session_state.to_dict()
    session_payload["env"] = {
        key: redact_env_value(key, value)
        for key, value in (session_payload.get("env") or {}).items()
    }
    project_runtime = _load_project_runtime_raw(getattr(config, "_project_config_path", None))
    project_runtime["env"] = {
        key: redact_env_value(key, value)
        for key, value in (project_runtime.get("env") or {}).items()
    }
    payload: dict[str, Any] = {
        "session_scope": {
            "key": scope.key,
            "project_path": scope.project_path,
            "local_cwd": scope.local_cwd,
            "state_file": str(scope.state_file),
        },
        "session_state": session_payload,
        "project_runtime": project_runtime,
        "effective_runtime": _runtime_to_dict(runtime),
        "project_selection": _build_project_selection_payload(config),
    }
    if include_layers:
        layers = build_runtime_layers(
            runtime,
            user_command=None,
            include_python_unbuffered=False,
        )
        for layer in layers:
            if layer.get("kind") == "env_export" and "value" in layer:
                layer["value"] = redact_env_value(str(layer.get("key") or ""), layer["value"])
        payload["layers"] = layers
    return payload


def _render_runtime_payload(payload: dict[str, Any], *, title: str, include_layers: bool) -> None:
    session_scope = payload["session_scope"]
    session_state = payload["session_state"]
    project_runtime = payload["project_runtime"]
    effective = payload["effective_runtime"]
    project_selection = payload["project_selection"]

    lines = [
        title,
        f"Session key: {session_scope['key']}",
        f"Project config: {session_scope['project_path'] or '<none>'}",
        f"Session file: {session_scope['state_file']}",
        "Session state:",
        f"  Cwd: {session_state.get('cwd') or '<unset>'}",
        f"  Venv: {session_state.get('venv') or '<unset>'}",
    ]
    session_paths = session_state.get("path_prepend") or []
    lines.append("  Path add:")
    if session_paths:
        lines.extend(f"    - {value}" for value in session_paths)
    else:
        lines.append("    - <none>")
    removed_paths = session_state.get("path_remove") or []
    lines.append("  Path remove:")
    if removed_paths:
        lines.extend(f"    - {value}" for value in removed_paths)
    else:
        lines.append("    - <none>")
    session_env = session_state.get("env") or {}
    lines.append("  Env set:")
    if session_env:
        lines.extend(f"    - {key}={value}" for key, value in session_env.items())
    else:
        lines.append("    - <none>")
    env_unset = session_state.get("env_unset") or []
    lines.append("  Env unset:")
    if env_unset:
        lines.extend(f"    - {key}" for key in env_unset)
    else:
        lines.append("    - <none>")

    lines.append("Project runtime config:")
    lines.append(f"  Cwd: {project_runtime.get('cwd', '<unset>')}")
    project_paths = project_runtime.get("path_prepend") or []
    lines.append("  Path prepend:")
    if project_paths:
        lines.extend(f"    - {value}" for value in project_paths)
    else:
        lines.append("    - <none>")
    lines.append(f"  Venv: {project_runtime.get('venv', '<unset>')}")
    project_env = project_runtime.get("env") or {}
    lines.append("  Env:")
    if project_env:
        lines.extend(f"    - {key}={value}" for key, value in project_env.items())
    else:
        lines.append("    - <none>")

    lines.append("Project selection:")
    project_order = project_selection.get("project_order") or []
    if project_order:
        resolved_by_id = {
            item["project_id"]: item.get("name") or item["project_id"]
            for item in project_selection.get("resolved", [])
        }
        for index, project_id in enumerate(project_order, 1):
            name = resolved_by_id.get(project_id)
            if name and name != project_id:
                lines.append(f"  {index}. {name} ({project_id})")
            else:
                lines.append(f"  {index}. {project_id}")
    else:
        lines.append("  Project order: <none>")
    unresolved = project_selection.get("unresolved") or []
    if unresolved:
        lines.append("  Unresolved:")
        lines.extend(f"    - {value}" for value in unresolved)
    if project_selection.get("resolution_error"):
        lines.append(f"  Resolution error: {project_selection['resolution_error']}")

    lines.append("Effective runtime:")
    lines.append(f"  Cwd: {effective['cwd'] or '<unset>'} [{effective['cwd_source']}]")
    lines.append("  Path prepend:")
    effective_paths = effective["path_prepend"]
    if effective_paths:
        lines.extend(f"    - {entry['value']} [{entry['source']}]" for entry in effective_paths)
    else:
        lines.append("    - <none>")
    lines.append(f"  Venv: {effective['venv']['mode']} [{effective['venv']['source']}]")
    if effective["venv"]["path"]:
        lines.append(f"  Venv path: {effective['venv']['path']}")
    lines.append("  Env:")
    effective_env = effective["env"]
    if effective_env:
        lines.extend(
            f"    - {entry['key']}={entry['value']} [{entry['source']}]" for entry in effective_env
        )
    else:
        lines.append("    - <none>")

    if include_layers:
        lines.append("Layers:")
        for layer in payload.get("layers", []):
            parts = [layer["kind"]]
            if "key" in layer:
                parts.append(str(layer["key"]))
            if "value" in layer:
                parts.append(str(layer["value"]))
            parts.append(f"[{layer['source']}]")
            lines.append(f"  - {' '.join(parts)}")

    click.echo("\n".join(lines))


def _load_runtime_config_and_state() -> tuple[Config, RuntimeSessionState]:
    config, _sources = Config.from_files_and_env(
        require_credentials=False, require_target_dir=False
    )
    scope = resolve_runtime_session_scope(config=config)
    return config, load_runtime_session_state(scope)


def _apply_session_mutation(mutator: Callable[[RuntimeSessionState], RuntimeSessionState]) -> None:
    config, state = _load_runtime_config_and_state()
    scope = resolve_runtime_session_scope(config=config)
    save_runtime_session_state(scope, mutator(state))


def _mutate_project_runtime(mutator: Callable[[tomlkit.TOMLDocument], None]) -> Path:
    path = _resolve_project_config_path()
    doc = _load_toml_doc(path)
    mutator(doc)
    _cleanup_runtime_table(doc)
    _save_toml_doc(path, doc)
    return path


def _emit_runtime_write_result(
    ctx: Context,
    *,
    scope: str,
    action: str,
    payload_extra: dict[str, Any] | None = None,
) -> None:
    payload = {"scope": scope, "action": action}
    if payload_extra:
        payload.update(payload_extra)
    emit_success(ctx, payload=payload, text=f"{action} ({scope})")


@click.group()
def runtime() -> None:
    """Inspect and mutate effective runtime state."""


@runtime.command("show")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format (table or json)",
)
@pass_context
def show_runtime(ctx: Context, output_format: str) -> None:
    """Show session overrides, project runtime config, and effective runtime."""
    effective_json = resolve_json_output(ctx, False)
    try:
        config, session_state = _load_runtime_config_and_state()
        payload = _build_runtime_payload(
            config=config,
            session_state=session_state,
            include_layers=False,
        )
        if effective_json:
            emit_success(ctx, payload=payload)
            return
        if output_format == "json":
            click.echo(json.dumps(payload, indent=2))
            return
        _render_runtime_payload(payload, title="Runtime state", include_layers=False)
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime.command("explain")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format (table or json)",
)
@pass_context
def explain_runtime(ctx: Context, output_format: str) -> None:
    """Explain the ordered runtime layers without executing a command."""
    effective_json = resolve_json_output(ctx, False)
    try:
        config, session_state = _load_runtime_config_and_state()
        payload = _build_runtime_payload(
            config=config,
            session_state=session_state,
            include_layers=True,
        )
        if effective_json:
            emit_success(ctx, payload=payload)
            return
        if output_format == "json":
            click.echo(json.dumps(payload, indent=2))
            return
        _render_runtime_payload(payload, title="Runtime explain", include_layers=True)
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime.group("set")
def runtime_set() -> None:
    """Set a runtime field."""


@runtime_set.command("cwd")
@click.argument("value")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@pass_context
def runtime_set_cwd(ctx: Context, value: str, scope: str) -> None:
    """Set the runtime cwd."""
    try:
        normalized = _require_non_empty(value, field_name="cwd")
        if scope == "session":
            _apply_session_mutation(lambda state: replace(state, cwd=normalized))
            _emit_runtime_write_result(
                ctx,
                scope=scope,
                action="set cwd",
                payload_extra={"value": normalized},
            )
            return
        path = _mutate_project_runtime(
            lambda doc: _ensure_runtime_table(doc).__setitem__("cwd", normalized)
        )
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="set cwd",
            payload_extra={"config_path": str(path), "value": normalized},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime_set.command("venv")
@click.argument("value")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@pass_context
def runtime_set_venv(ctx: Context, value: str, scope: str) -> None:
    """Set the runtime venv mode or path."""
    try:
        normalized = _require_non_empty(value, field_name="venv")
        if scope == "session":
            _apply_session_mutation(lambda state: replace(state, venv=normalized))
            _emit_runtime_write_result(
                ctx,
                scope=scope,
                action="set venv",
                payload_extra={"value": normalized},
            )
            return
        path = _mutate_project_runtime(
            lambda doc: _ensure_runtime_table(doc).__setitem__("venv", normalized)
        )
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="set venv",
            payload_extra={"config_path": str(path), "value": normalized},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime.group("unset")
def runtime_unset() -> None:
    """Unset a runtime field."""


@runtime_unset.command("cwd")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@pass_context
def runtime_unset_cwd(ctx: Context, scope: str) -> None:
    """Remove the runtime cwd override."""
    try:
        if scope == "session":
            _apply_session_mutation(lambda state: replace(state, cwd=None))
            _emit_runtime_write_result(ctx, scope=scope, action="unset cwd")
            return
        path = _mutate_project_runtime(lambda doc: _ensure_runtime_table(doc).pop("cwd", None))
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="unset cwd",
            payload_extra={"config_path": str(path)},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime_unset.command("venv")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@pass_context
def runtime_unset_venv(ctx: Context, scope: str) -> None:
    """Remove the runtime venv override."""
    try:
        if scope == "session":
            _apply_session_mutation(lambda state: replace(state, venv=None))
            _emit_runtime_write_result(ctx, scope=scope, action="unset venv")
            return
        path = _mutate_project_runtime(lambda doc: _ensure_runtime_table(doc).pop("venv", None))
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="unset venv",
            payload_extra={"config_path": str(path)},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime.group("path")
def runtime_path() -> None:
    """Mutate runtime PATH prepends."""


@runtime_path.command("add")
@click.argument("value")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@pass_context
def runtime_path_add(ctx: Context, value: str, scope: str) -> None:
    """Add a path to runtime PATH prepends."""
    try:
        normalized = _require_non_empty(value, field_name="path")
        if scope == "session":

            def _mutator(state: RuntimeSessionState) -> RuntimeSessionState:
                current = [item for item in state.path_prepend if item != normalized]
                removed = [item for item in state.path_remove if item != normalized]
                return replace(
                    state,
                    path_prepend=tuple([normalized, *current]),
                    path_remove=tuple(removed),
                )

            _apply_session_mutation(_mutator)
            _emit_runtime_write_result(
                ctx,
                scope=scope,
                action="path add",
                payload_extra={"value": normalized},
            )
            return

        def _project_mutator(doc: tomlkit.TOMLDocument) -> None:
            runtime_table = _ensure_runtime_table(doc)
            current = runtime_table.get("path_prepend")
            items = [str(item) for item in current] if isinstance(current, list) else []
            items = [item for item in items if item != normalized]
            runtime_table["path_prepend"] = [normalized, *items]

        path = _mutate_project_runtime(_project_mutator)
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="path add",
            payload_extra={"config_path": str(path), "value": normalized},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime_path.command("remove")
@click.argument("value")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@pass_context
def runtime_path_remove(ctx: Context, value: str, scope: str) -> None:
    """Remove a path from runtime PATH prepends."""
    try:
        normalized = _require_non_empty(value, field_name="path")
        if scope == "session":

            def _mutator(state: RuntimeSessionState) -> RuntimeSessionState:
                current = tuple(item for item in state.path_prepend if item != normalized)
                removed = _normalize_text_list([*state.path_remove, normalized])
                return replace(state, path_prepend=current, path_remove=tuple(removed))

            _apply_session_mutation(_mutator)
            _emit_runtime_write_result(
                ctx,
                scope=scope,
                action="path remove",
                payload_extra={"value": normalized},
            )
            return

        def _project_mutator(doc: tomlkit.TOMLDocument) -> None:
            runtime_table = _ensure_runtime_table(doc)
            current = runtime_table.get("path_prepend")
            if isinstance(current, list):
                items = [str(item) for item in current if str(item) != normalized]
                if items:
                    runtime_table["path_prepend"] = items
                else:
                    runtime_table.pop("path_prepend", None)

        path = _mutate_project_runtime(_project_mutator)
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="path remove",
            payload_extra={"config_path": str(path), "value": normalized},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime.group("env")
def runtime_env() -> None:
    """Mutate runtime environment overrides."""


@runtime_env.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@pass_context
def runtime_env_set(ctx: Context, key: str, value: str, scope: str) -> None:
    """Set a runtime environment variable."""
    try:
        normalized_key = _require_env_key(key)
        if scope == "session":

            def _mutator(state: RuntimeSessionState) -> RuntimeSessionState:
                env = dict(state.env)
                env[normalized_key] = value
                env_unset = tuple(item for item in state.env_unset if item != normalized_key)
                return replace(state, env=env, env_unset=env_unset)

            _apply_session_mutation(_mutator)
            _emit_runtime_write_result(
                ctx,
                scope=scope,
                action="env set",
                payload_extra={"key": normalized_key},
            )
            return

        def _project_mutator(doc: tomlkit.TOMLDocument) -> None:
            runtime_table = _ensure_runtime_table(doc)
            if "env" not in runtime_table or not isinstance(runtime_table["env"], dict):
                runtime_table["env"] = tomlkit.table()
            runtime_table["env"][normalized_key] = value

        path = _mutate_project_runtime(_project_mutator)
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="env set",
            payload_extra={"config_path": str(path), "key": normalized_key},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime_env.command("unset")
@click.argument("key")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@pass_context
def runtime_env_unset(ctx: Context, key: str, scope: str) -> None:
    """Unset a runtime environment variable."""
    try:
        normalized_key = _require_env_key(key)
        if scope == "session":

            def _mutator(state: RuntimeSessionState) -> RuntimeSessionState:
                env = dict(state.env)
                env.pop(normalized_key, None)
                env_unset = _normalize_text_list([*state.env_unset, normalized_key])
                return replace(state, env=env, env_unset=tuple(env_unset))

            _apply_session_mutation(_mutator)
            _emit_runtime_write_result(
                ctx,
                scope=scope,
                action="env unset",
                payload_extra={"key": normalized_key},
            )
            return

        def _project_mutator(doc: tomlkit.TOMLDocument) -> None:
            runtime_table = _ensure_runtime_table(doc)
            env_table = runtime_table.get("env")
            if isinstance(env_table, dict):
                env_table.pop(normalized_key, None)

        path = _mutate_project_runtime(_project_mutator)
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="env unset",
            payload_extra={"config_path": str(path), "key": normalized_key},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime.group("project-order")
def runtime_project_order() -> None:
    """Inspect and mutate project selection order."""


@runtime_project_order.command("show")
@pass_context
def runtime_project_order_show(ctx: Context) -> None:
    """Show project selection order from project config."""
    effective_json = resolve_json_output(ctx, False)
    try:
        config, _state = _load_runtime_config_and_state()
        payload = _build_project_selection_payload(config)
        if effective_json:
            emit_success(ctx, payload=payload)
            return

        order = payload.get("project_order") or []
        if not order:
            click.echo("Project order: <none>")
            return

        click.echo("Project order:")
        resolved_by_id = {
            item["project_id"]: item.get("name") or item["project_id"]
            for item in payload.get("resolved", [])
        }
        for index, project_id in enumerate(order, 1):
            name = resolved_by_id.get(project_id)
            if name and name != project_id:
                click.echo(f"  {index}. {name} ({project_id})")
            else:
                click.echo(f"  {index}. {project_id}")
        unresolved = payload.get("unresolved") or []
        if unresolved:
            click.echo("Unresolved:")
            for value in unresolved:
                click.echo(f"  - {value}")
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime_project_order.command("set")
@click.argument("projects", nargs=-1, required=True)
@pass_context
def runtime_project_order_set(ctx: Context, projects: tuple[str, ...]) -> None:
    """Replace project selection order."""
    try:
        config, _state = _load_runtime_config_and_state()
        available = project_order_utils.fetch_current_account_projects(config)
        project_ids = project_order_utils.resolve_project_refs(projects, projects=available)
        path = project_order_utils.write_project_order(project_ids)
        _emit_runtime_write_result(
            ctx,
            scope="project",
            action="project-order set",
            payload_extra={"config_path": str(path), "project_order": project_ids},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime_project_order.command("add")
@click.argument("project")
@pass_context
def runtime_project_order_add(ctx: Context, project: str) -> None:
    """Move a project to the front of project selection order."""
    try:
        config, _state = _load_runtime_config_and_state()
        available = project_order_utils.fetch_current_account_projects(config)
        resolved = project_order_utils.resolve_project_ref(project, projects=available)
        current = [
            entry
            for entry in project_order_utils.normalize_project_order(
                project_order_utils.read_project_order(),
                projects=available,
            )
            if entry != resolved.project_id
        ]
        project_ids = [resolved.project_id, *current]
        path = project_order_utils.write_project_order(project_ids)
        _emit_runtime_write_result(
            ctx,
            scope="project",
            action="project-order add",
            payload_extra={"config_path": str(path), "project_order": project_ids},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime_project_order.command("remove")
@click.argument("project")
@pass_context
def runtime_project_order_remove(ctx: Context, project: str) -> None:
    """Remove a project from project selection order."""
    try:
        current = project_order_utils.read_project_order()
        normalized = str(project or "").strip()
        if not normalized:
            raise ConfigError("Project cannot be empty.")

        has_legacy_entries = any(
            not project_order_utils.looks_like_stable_project_id(entry) for entry in current
        )
        if project_order_utils.looks_like_stable_project_id(normalized) and normalized in current:
            project_id = normalized
            if has_legacy_entries:
                config, _state = _load_runtime_config_and_state()
                available = project_order_utils.fetch_current_account_projects(config)
                current = project_order_utils.normalize_project_order_preserving_unresolved(
                    current,
                    projects=available,
                )
        else:
            config, _state = _load_runtime_config_and_state()
            available = project_order_utils.fetch_current_account_projects(config)
            resolved = project_order_utils.resolve_project_ref(project, projects=available)
            project_id = resolved.project_id
            current = project_order_utils.normalize_project_order_preserving_unresolved(
                current,
                projects=available,
            )

        project_ids = [entry for entry in current if entry != project_id]
        path = project_order_utils.write_project_order(project_ids)
        payload_extra: dict[str, Any] = {"config_path": str(path), "project_order": project_ids}
        warning: str | None = None
        effective, source, source_path = _effective_project_order_layer()
        if project_id in effective:
            warning = (
                f"project-order remove only changed the project config; {project_id!r} is still "
                f"effective from {_project_order_layer_label(source, source_path)}."
            )
            payload_extra.update(
                {
                    "removed": False,
                    "remaining_project_order": effective,
                    "remaining_source": source,
                    "remaining_config_path": source_path,
                    "warning": warning,
                }
            )
        _emit_runtime_write_result(
            ctx,
            scope="project",
            action="project-order remove",
            payload_extra=payload_extra,
        )
        if warning and not ctx.json_output:
            click.echo(click.style(f"Warning: {warning}", fg="yellow"))
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime_project_order.command("reset")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Reset without an interactive confirmation (alias: --force)",
)
@pass_context
def runtime_project_order_reset(ctx: Context, yes: bool) -> None:
    """Clear project selection order."""
    subject = "clear the project selection order"
    if not confirmed_or_error(ctx, assume_yes=yes, subject=subject):
        raise click.Abort()

    try:
        path = project_order_utils.write_project_order([])
        payload_extra: dict[str, Any] = {"config_path": str(path), "project_order": []}
        effective, source, source_path = _effective_project_order_layer()
        warning: str | None = None
        if effective:
            # The project-level key was just cleared, so any surviving order is a
            # false "cleared" — including one still attributed to the project layer.
            warning = _project_order_not_cleared_warning(
                action="project-order reset", source=source, config_path=source_path
            )
            payload_extra.update(
                {
                    "cleared": False,
                    "remaining_project_order": effective,
                    "remaining_source": source,
                    "remaining_config_path": source_path,
                    "warning": warning,
                }
            )
        _emit_runtime_write_result(
            ctx,
            scope="project",
            action="project-order reset",
            payload_extra=payload_extra,
        )
        if warning and not ctx.json_output:
            click.echo(click.style(f"Warning: {warning}", fg="yellow"))
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)


@runtime.command("reset")
@click.option("--scope", type=click.Choice(["session", "project"]), default="session")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Reset without an interactive confirmation (alias: --force)",
)
@pass_context
def runtime_reset(ctx: Context, scope: str, yes: bool) -> None:
    """Reset runtime-managed state for the selected scope."""
    subject = f"reset the {scope} runtime state"
    if not confirmed_or_error(ctx, assume_yes=yes, subject=subject):
        raise click.Abort()

    try:
        if scope == "session":
            config, _state = _load_runtime_config_and_state()
            clear_runtime_session_state(resolve_runtime_session_scope(config=config))
            _emit_runtime_write_result(ctx, scope=scope, action="reset")
            return
        path = _mutate_project_runtime(lambda doc: doc.pop("remote_runtime", None))
        _emit_runtime_write_result(
            ctx,
            scope=scope,
            action="reset",
            payload_extra={"config_path": str(path)},
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "Error", str(exc), EXIT_GENERAL_ERROR)
