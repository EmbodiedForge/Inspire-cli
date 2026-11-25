"""Helpers for notebook post-start commands and scripts."""

from __future__ import annotations

import base64
import shlex
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from inspire.config import (
    Config,
    EffectiveRemoteRuntime,
    build_runtime_activate_command,
    build_runtime_cd_command,
    build_runtime_export_commands,
    load_runtime_session_state_for_config,
    resolve_effective_remote_runtime,
)

POST_START_LOG = "/tmp/inspire-notebook-post-start.log"
POST_START_PID_FILE = "/tmp/inspire-notebook-post-start.pid"
POST_START_SCRIPT_PATH = "/tmp/inspire-notebook-post-start.sh"
POST_START_STARTED_MARKER = "INSPIRE_NOTEBOOK_POST_START_STARTED"
POST_START_MODE_START = "start"
POST_START_MODE_RUN = "run"
POST_START_MODES = {POST_START_MODE_START, POST_START_MODE_RUN}
NO_WAIT_POST_START_WARNING = (
    "Note: --no-wait requested, but a notebook post-start action is configured. "
    "Waiting anyway so the post-start action can run. "
    "Remove the post-start action or set notebook_post_start=none to return immediately."
)
_POST_START_DISABLED_VALUES = {"0", "disable", "disabled", "false", "none", "off"}
_REMOVED_POST_START_VALUES = {"keepalive"}


@dataclass(frozen=True)
class NotebookPostStartSpec:
    label: str
    command: str
    log_path: str
    pid_file: str
    completion_marker: str
    mode: str = POST_START_MODE_START
    requires_gpu: bool = False


def _normalize_post_start_value(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _is_disabled_post_start(value: str | None) -> bool:
    text = _normalize_post_start_value(value)
    return bool(text and text.lower() in _POST_START_DISABLED_VALUES)


def _is_removed_post_start_value(value: str | None) -> bool:
    text = _normalize_post_start_value(value)
    return bool(text and text.lower() in _REMOVED_POST_START_VALUES)


def _resolve_post_start_mode(config: Config, post_start_mode: str | None) -> str:
    resolved = _normalize_post_start_value(post_start_mode)
    if resolved is None:
        resolved = _normalize_post_start_value(getattr(config, "notebook_post_start_mode", None))
    if resolved is None:
        return POST_START_MODE_START
    mode = resolved.lower()
    if mode not in POST_START_MODES:
        raise ValueError("Post-start mode must be one of: start, run.")
    return mode


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _post_start_log_path(run_id: str) -> str:
    return f"/tmp/inspire-notebook-post-start-{run_id}.log"


def _post_start_pid_file(run_id: str) -> str:
    return f"/tmp/inspire-notebook-post-start-{run_id}.pid"


def _post_start_script_path(run_id: str) -> str:
    return f"/tmp/inspire-notebook-post-start-{run_id}.sh"


def _post_start_marker(run_id: str) -> str:
    return f"{POST_START_STARTED_MARKER}_{run_id}"


def _build_background_command(
    command_text: str,
    *,
    log_path: str,
    pid_file: str,
    completion_marker: str,
) -> str:
    pid_file_q = shlex.quote(pid_file)
    marker_print = _build_marker_print_command(completion_marker)
    inner_command = f"echo $$ > {pid_file_q}; exec bash -lc {shlex.quote(command_text)}"
    return " ".join(
        [
            f'if [ -f {pid_file_q} ] && kill -0 "$(cat {pid_file_q})" 2>/dev/null; then',
            f"{marker_print};",
            "exit 0;",
            "fi;",
            f"rm -f {pid_file_q};",
            f"nohup bash -lc {shlex.quote(inner_command)} > {shlex.quote(log_path)} 2>&1 < /dev/null &",
            "POST_START_STARTED=0;",
            "for _ in 1 2 3 4 5; do",
            f'if [ -f {pid_file_q} ] && kill -0 "$(cat {pid_file_q})" 2>/dev/null; then POST_START_STARTED=1;',
            f"{marker_print};",
            "break;",
            "fi;",
            "sleep 1;",
            "done;",
            '[ "$POST_START_STARTED" -eq 1 ]',
        ]
    )


def _build_marker_print_command(completion_marker: str) -> str:
    """Print the marker without embedding it contiguously in terminal input."""
    split_at = max(1, len(completion_marker) // 2)
    first = completion_marker[:split_at]
    second = completion_marker[split_at:]
    return f"printf '%s%s\\n' {shlex.quote(first)} {shlex.quote(second)}"


def _build_exit_marker_print_command(completion_marker: str) -> str:
    split_at = max(1, len(completion_marker) // 2)
    first = completion_marker[:split_at]
    second = completion_marker[split_at:]
    return f"printf '%s%s:%s\\n' {shlex.quote(first)} {shlex.quote(second)} \"$POST_START_RC\""


def _build_foreground_command(
    command_text: str,
    *,
    log_path: str,
    pid_file: str,
    completion_marker: str,
) -> str:
    pid_file_q = shlex.quote(pid_file)
    log_path_q = shlex.quote(log_path)
    marker_print = _build_exit_marker_print_command(completion_marker)
    return " ".join(
        [
            f"rm -f {pid_file_q};",
            f"echo $$ > {pid_file_q};",
            f"bash -lc {shlex.quote(command_text)} > {log_path_q} 2>&1;",
            "POST_START_RC=$?;",
            f"{marker_print};",
            'exit "$POST_START_RC"',
        ]
    )


def _build_script_command(
    script_text: str,
    *,
    log_path: str,
    pid_file: str,
    script_path: str,
    completion_marker: str,
    mode: str,
    remote_preamble: str = "",
) -> str:
    encoded_script = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
    launch_script = (
        f'{remote_preamble}exec bash "$SCRIPT_PATH"'
        if remote_preamble
        else 'exec bash "$SCRIPT_PATH"'
    )
    command_text = (
        f"SCRIPT_PATH={shlex.quote(script_path)}; "
        f'printf %s {shlex.quote(encoded_script)} | base64 -d > "$SCRIPT_PATH"; '
        'chmod +x "$SCRIPT_PATH"; '
        f"{launch_script}"
    )
    if mode == POST_START_MODE_RUN:
        return _build_foreground_command(
            command_text,
            log_path=log_path,
            pid_file=pid_file,
            completion_marker=completion_marker,
        )
    return _build_background_command(
        command_text,
        log_path=log_path,
        pid_file=pid_file,
        completion_marker=completion_marker,
    )


def _build_command_spec_with_env(
    command_text: str, *, env_exports: str, mode: str
) -> NotebookPostStartSpec:
    full_command = f"{env_exports}{command_text}" if env_exports else command_text
    run_id = _new_run_id()
    log_path = _post_start_log_path(run_id)
    pid_file = _post_start_pid_file(run_id)
    completion_marker = _post_start_marker(run_id)
    builder = (
        _build_foreground_command if mode == POST_START_MODE_RUN else _build_background_command
    )
    return NotebookPostStartSpec(
        label="notebook post-start command",
        command=builder(
            full_command,
            log_path=log_path,
            pid_file=pid_file,
            completion_marker=completion_marker,
        ),
        log_path=log_path,
        pid_file=pid_file,
        completion_marker=completion_marker,
        mode=mode,
    )


def _resolve_post_start_runtime(config: Config) -> EffectiveRemoteRuntime:
    runtime = resolve_effective_remote_runtime(
        config,
        require_cwd=False,
        session_state=load_runtime_session_state_for_config(config),
    )
    if runtime.cwd_source != "defaults.target_dir":
        return runtime

    venv_path = runtime.venv_path
    if runtime.venv_mode == "auto" and runtime.venv_source == "default":
        venv_path = None
    return replace(runtime, cwd=None, cwd_source="unset", venv_path=venv_path)


def _build_runtime_prefix(config: Config) -> str:
    runtime = _resolve_post_start_runtime(config)
    runtime_prefix_parts = build_runtime_export_commands(runtime)
    activate_command = build_runtime_activate_command(runtime)
    if activate_command:
        runtime_prefix_parts.append(activate_command)
    cd_command = build_runtime_cd_command(runtime)
    if cd_command:
        runtime_prefix_parts.append(cd_command)
    runtime_prefix = " && ".join(runtime_prefix_parts)
    if runtime_prefix:
        runtime_prefix = f"{runtime_prefix} && "
    return runtime_prefix


def _build_script_spec_with_env(
    script_path: Path, *, env_exports: str, mode: str
) -> NotebookPostStartSpec:
    try:
        script_text = script_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Failed to read post-start script '{script_path}': {exc}") from exc

    if not script_text.strip():
        raise ValueError(f"Post-start script '{script_path}' is empty.")

    run_id = _new_run_id()
    log_path = _post_start_log_path(run_id)
    pid_file = _post_start_pid_file(run_id)
    completion_marker = _post_start_marker(run_id)
    return NotebookPostStartSpec(
        label=f"notebook post-start script ({script_path.name})",
        command=_build_script_command(
            script_text,
            log_path=log_path,
            pid_file=pid_file,
            script_path=_post_start_script_path(run_id),
            completion_marker=completion_marker,
            mode=mode,
            remote_preamble=env_exports,
        ),
        log_path=log_path,
        pid_file=pid_file,
        completion_marker=completion_marker,
        mode=mode,
    )


def resolve_notebook_post_start_spec(
    *,
    config: Config,
    post_start: str | None,
    post_start_script: Path | None,
    post_start_mode: str | None = None,
    keepalive: bool | None = None,
) -> NotebookPostStartSpec | None:
    if keepalive is not None:
        raise ValueError(
            "The keepalive notebook post-start preset has been removed. "
            "Use post_start='none', --post-start '<shell command>', "
            "or --post-start-script PATH."
        )

    if post_start_script is not None:
        mode = _resolve_post_start_mode(config, post_start_mode)
        runtime_prefix = _build_runtime_prefix(config)
        return _build_script_spec_with_env(
            post_start_script,
            env_exports=runtime_prefix,
            mode=mode,
        )

    resolved_value = _normalize_post_start_value(post_start)
    if resolved_value is None:
        resolved_value = _normalize_post_start_value(getattr(config, "notebook_post_start", None))

    if _is_disabled_post_start(resolved_value):
        return None
    if _is_removed_post_start_value(resolved_value):
        raise ValueError(
            "The 'keepalive' notebook post-start preset has been removed. "
            "Use notebook_post_start=none, --post-start '<shell command>', "
            "or --post-start-script PATH."
        )
    if resolved_value is None:
        return None
    mode = _resolve_post_start_mode(config, post_start_mode)
    runtime_prefix = _build_runtime_prefix(config)
    return _build_command_spec_with_env(resolved_value, env_exports=runtime_prefix, mode=mode)


def parse_post_start_exit_code(stdout: str, completion_marker: str) -> int:
    prefix = f"{completion_marker}:"
    for line in reversed(str(stdout or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix) :].strip()
        try:
            return int(value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid notebook post-start marker: {stripped}") from exc
    raise RuntimeError("Notebook post-start command completed without an exit-code marker.")


__all__ = [
    "NotebookPostStartSpec",
    "NO_WAIT_POST_START_WARNING",
    "POST_START_MODE_RUN",
    "POST_START_MODE_START",
    "POST_START_LOG",
    "POST_START_PID_FILE",
    "POST_START_SCRIPT_PATH",
    "POST_START_STARTED_MARKER",
    "parse_post_start_exit_code",
    "resolve_notebook_post_start_spec",
]
