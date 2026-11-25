"""Bridge exec command -- execute a shell command on the Bridge runner."""

from __future__ import annotations

import atexit
import base64
import datetime
import logging
import os
import posixpath
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.cli.logging_setup import redact_env_value, redact_text
from inspire.config import (
    Config,
    ConfigError,
    EffectiveRemoteRuntime,
    build_runtime_activate_command,
    build_runtime_cd_command,
    build_runtime_export_commands,
    build_runtime_layers,
    load_runtime_session_state_for_config,
    resolve_effective_remote_runtime,
)
from inspire.bridge.tunnel import (
    BridgeProfile,
    TunnelConfig,
    TunnelNotAvailableError,
    is_paramiko_available,
    is_tunnel_available,
    load_tunnel_config,
    run_ssh_command,
    run_ssh_command_detached,
    run_ssh_command_pooled,
    run_ssh_command_streaming,
    run_ssh_command_streaming_pooled,
)
from inspire.bridge.tunnel.scp import run_scp_transfer
from inspire.bridge.tunnel.ssh_helpers import remote_bash_invocation
from inspire.cli.utils.common import json_option
from inspire.cli.utils.bridge_transport import BRIDGE_TRANSPORTS, select_bridge_transport
from inspire.cli.utils.errors import emit_error as _emit_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.notebook_cli import require_web_session
from inspire.cli.utils.output import (
    emit_error,
    emit_info,
    emit_success,
    emit_warning,
)
from inspire.cli.utils.tunnel_reconnect import (
    NotebookBridgeReconnectState,
    NotebookBridgeReconnectStatus,
    attempt_notebook_bridge_rebuild,
    load_ssh_public_key_material,
    rebuild_notebook_bridge_profile,
    should_attempt_ssh_reconnect,
)
from inspire.config.ssh_runtime import resolve_ssh_runtime_config
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module

logger = logging.getLogger(__name__)
_RUNNING_NOTEBOOK_STATUS = "RUNNING"

# Track temp files created for long stderr so they can be cleaned up on exit.
_STDERR_TEMP_FILES: set[str] = set()


def _cleanup_stderr_temp_files() -> None:
    """Remove any lingering stderr temp files on process exit."""
    for path in list(_STDERR_TEMP_FILES):
        try:
            os.unlink(path)
        except OSError:
            pass
        _STDERR_TEMP_FILES.discard(path)


atexit.register(_cleanup_stderr_temp_files)


# Track temp files created for stdin script content so they can be cleaned up on exit.
_STDIN_TEMP_FILES: set[str] = set()


def _cleanup_stdin_temp_files() -> None:
    """Remove any lingering stdin temp files on process exit."""
    for path in list(_STDIN_TEMP_FILES):
        try:
            os.unlink(path)
        except OSError:
            pass
        _STDIN_TEMP_FILES.discard(path)


atexit.register(_cleanup_stdin_temp_files)


@dataclass(frozen=True)
class ExecFileStage:
    local_path: Path
    remote_dir: str
    remote_path: str
    scp_remote_path: str


@dataclass(frozen=True)
class ExecFileStageFailure:
    message: str
    returncode: int | None = None


def _sanitize_command_for_filename(command: str) -> str:
    sanitized = command[:30]
    sanitized = sanitized.replace(" ", "_").replace("/", "_").replace(".", "_")
    return sanitized


def _build_remote_command(
    *,
    command: str,
    runtime: EffectiveRemoteRuntime,
    log_path: Optional[str] = None,
) -> str:
    command_parts = build_runtime_export_commands(runtime)
    command_parts.append("export PYTHONUNBUFFERED=1")

    activate_command = build_runtime_activate_command(runtime)
    if activate_command:
        command_parts.append(activate_command)

    cd_command = build_runtime_cd_command(runtime)
    if cd_command:
        command_parts.append(cd_command)

    command_parts.append(command)
    base_command = " && ".join(command_parts)
    if log_path is not None:
        quoted_log_path = shlex.quote(log_path)
        quoted_log_dir = shlex.quote(posixpath.dirname(log_path))
        base_command = (
            f"mkdir -p {quoted_log_dir} && "
            f"set -o pipefail && ({base_command}) 2>&1 | tee {quoted_log_path}"
        )
    return base_command


def _build_detached_command(*, command: str, log_path: Optional[str] = None) -> str:
    quoted_command = shlex.quote(command)
    remote_bash = remote_bash_invocation("--noprofile", "--norc", "-lc")
    redirect = ">/dev/null 2>&1"
    setup_prefix = ""
    if log_path is not None:
        quoted_log_path = shlex.quote(log_path)
        quoted_log_dir = shlex.quote(posixpath.dirname(log_path))
        setup_prefix = f"mkdir -p {quoted_log_dir} && "
        redirect = f">{quoted_log_path} 2>&1"
    return f"{setup_prefix}nohup {remote_bash} {quoted_command} </dev/null {redirect} &"


def _build_log_path(runtime: EffectiveRemoteRuntime, command: str) -> str | None:
    if runtime.cwd is None:
        return None

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    sanitized = _sanitize_command_for_filename(command)
    return posixpath.join(runtime.cwd, ".inspire", "logs", f"exec-{timestamp}-{sanitized}.log")


def _resolve_exec_file(ctx: Context, script_file: str) -> Path:
    path = Path(script_file).expanduser()
    if not path.exists():
        _emit_error(ctx, "FileNotFound", f"Local script not found: {script_file}")
        sys.exit(EXIT_GENERAL_ERROR)
    if path.is_dir():
        _emit_error(ctx, "InvalidFile", f"Local script is a directory: {script_file}")
        sys.exit(EXIT_GENERAL_ERROR)
    if not path.is_file():
        _emit_error(ctx, "InvalidFile", f"Local script is not a regular file: {script_file}")
        sys.exit(EXIT_GENERAL_ERROR)
    return path


def _build_exec_file_remote_paths(
    *,
    runtime: EffectiveRemoteRuntime,
    local_path: Path,
) -> tuple[str, str, str]:
    if runtime.cwd is None:
        raise ConfigError("bridge exec --file requires a resolved remote working directory.")
    suffix = uuid.uuid4().hex[:12]
    sanitized_name = _sanitize_command_for_filename(local_path.name) or "script.sh"
    remote_dir = posixpath.join(".inspire", "tmp", "exec", suffix)
    remote_path = posixpath.join(remote_dir, sanitized_name)
    scp_remote_path = _build_exec_file_scp_remote_path(
        runtime_cwd=runtime.cwd,
        relative_remote_path=remote_path,
    )
    return remote_dir, remote_path, scp_remote_path


def _build_exec_file_scp_remote_path(*, runtime_cwd: str, relative_remote_path: str) -> str:
    relative_path = str(relative_remote_path).lstrip("/")
    cwd = str(runtime_cwd or "").strip()
    if cwd == "~":
        return relative_path
    if cwd.startswith("~/"):
        return posixpath.normpath(posixpath.join(cwd[2:], relative_path))
    return posixpath.normpath(posixpath.join(cwd, relative_path))


def _build_exec_file_command(*, remote_path: str, script_args: tuple[str, ...]) -> str:
    command = (
        f"__inspire_exec_script={shlex.quote(remote_path)}; "
        "trap 'rm -f \"$__inspire_exec_script\"' EXIT; "
        f'{remote_bash_invocation("--noprofile", "--norc")} "$__inspire_exec_script"'
    )
    if script_args:
        command = f"{command} {shlex.join(script_args)}"
    return command


def _stage_exec_file(
    *,
    runtime: EffectiveRemoteRuntime,
    local_path: Path,
    remote_dir: str,
    scp_remote_path: str,
    bridge_name: str,
    tunnel_config: TunnelConfig,
    timeout: int,
    run_ssh_command_fn: Callable[..., object] = run_ssh_command,
    keepalive: bool = True,
) -> ExecFileStageFailure | None:
    cd_command = build_runtime_cd_command(runtime)
    mkdir_command = f"mkdir -p {shlex.quote(remote_dir)}"
    if cd_command:
        mkdir_command = f"{cd_command} && {mkdir_command}"

    mkdir_result = run_ssh_command_fn(
        command=mkdir_command,
        bridge_name=bridge_name,
        config=tunnel_config,
        timeout=timeout,
        capture_output=True,
        keepalive=keepalive,
    )
    if getattr(mkdir_result, "returncode", 1) != 0:
        detail = (
            getattr(mkdir_result, "stderr", "") or getattr(mkdir_result, "stdout", "") or ""
        ).strip()
        message = f"Failed to prepare remote script directory: {remote_dir}"
        if detail:
            message = f"{message}: {detail.splitlines()[-1][:400]}"
        return ExecFileStageFailure(
            message=message,
            returncode=getattr(mkdir_result, "returncode", None),
        )

    scp_result = run_scp_transfer(
        local_path=str(local_path),
        remote_path=scp_remote_path,
        download=False,
        recursive=False,
        bridge_name=bridge_name,
        config=tunnel_config,
        timeout=timeout,
    )
    if getattr(scp_result, "returncode", 1) != 0:
        detail = (
            getattr(scp_result, "stderr", "") or getattr(scp_result, "stdout", "") or ""
        ).strip()
        message = f"Failed to upload local script: {local_path}"
        if detail:
            message = f"{message}: {detail.splitlines()[-1][:400]}"
        return ExecFileStageFailure(
            message=message,
            returncode=getattr(scp_result, "returncode", None),
        )

    return None


def _render_exec_explain(
    *,
    ctx: Context,
    bridge: Optional[str],
    transport: str,
    stdin_mode: bool,
    runtime: EffectiveRemoteRuntime,
    full_command: str,
    user_command: str,
    command_source: str = "argv",
    local_file: Optional[str] = None,
    remote_file: Optional[str] = None,
) -> None:
    layers = build_runtime_layers(
        runtime,
        user_command=user_command,
        include_python_unbuffered=True,
    )
    for layer in layers:
        value = layer.get("value")
        if value is None:
            continue
        key = str(layer.get("key") or "")
        if layer.get("kind") == "env_export" and key:
            layer["value"] = redact_env_value(key, value)
        elif isinstance(value, str):
            layer["value"] = redact_text(value)
    env_entries = [
        {
            "key": key,
            "value": redact_env_value(key, value),
            "source": runtime.env_sources.get(key, "remote_env"),
        }
        for key, value in runtime.env.items()
    ]
    safe_full_command = redact_text(full_command)
    payload = {
        "bridge": bridge or "default",
        "transport": transport,
        "stdin_mode": "passthrough" if stdin_mode else "none",
        "command_source": command_source,
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
        "env": env_entries,
        "layers": layers,
        "final_remote_command": safe_full_command,
    }
    if local_file is not None:
        payload["local_file"] = local_file
    if remote_file is not None:
        payload["remote_file"] = remote_file
    if ctx.json_output:
        emit_success(ctx, payload=payload)
        return

    lines = [
        "Bridge exec runtime",
        f"Bridge: {bridge or 'default'}",
        f"Transport: {transport}",
        f"Stdin: {'passthrough' if stdin_mode else 'none'}",
        f"Command source: {command_source}",
        f"Cwd: {runtime.cwd or '<unset>'} [{runtime.cwd_source}]",
        "Path prepend:",
    ]
    if runtime.path_prepend:
        lines.extend(
            f"  - {path} [{source}]"
            for path, source in zip(runtime.path_prepend, runtime.path_prepend_sources, strict=True)
        )
    else:
        lines.append("  - <none>")

    if local_file is not None:
        lines.append(f"Local file: {local_file}")
    if remote_file is not None:
        lines.append(f"Remote file: {remote_file}")

    lines.append(f"Venv: {runtime.venv_mode} [{runtime.venv_source}]")
    if runtime.venv_path:
        lines.append(f"Venv path: {runtime.venv_path}")

    lines.append("Env:")
    if env_entries:
        lines.extend(
            f"  - {entry['key']}={entry['value']} [{entry['source']}]" for entry in env_entries
        )
    else:
        lines.append("  - <none>")

    lines.append("Final remote command:")
    lines.append(safe_full_command)
    click.echo("\n".join(lines))


def _normalize_exec_command(command_parts: tuple[str, ...]) -> str:
    if not command_parts:
        raise click.UsageError("Provide a command to execute, or pipe script content via stdin.")
    if len(command_parts) == 1:
        # Preserve legacy behavior when users pass a single quoted command string.
        return command_parts[0]
    return shlex.join(command_parts)


def _should_auto_passthrough_stdin() -> bool:
    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return False
    try:
        return not stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def _verbose_output(ctx: Context) -> bool:
    return not ctx.json_output and ctx.debug


_SSH_DROP_AFTER_OUTPUT_HINT = (
    "SSH connection dropped after the remote command already produced output. "
    "The command was not retried to avoid repeating remote side effects."
)

# Why a disconnect-style exit code (SSH 255) is, or is not, retried automatically.
_SSH_RETRY_REASON_RETRY = "retry"
_SSH_RETRY_REASON_OUTPUT_SEEN = "output_seen"
_SSH_RETRY_REASON_TUNNEL_ALIVE = "tunnel_alive"
_SSH_RETRY_REASON_NOT_DISCONNECT = "not_disconnect_code"


def _emit_command_failed(ctx: Context, *, returncode: int, hint: Optional[str] = None) -> int:
    return _emit_error(
        ctx, "CommandFailed", f"Command failed with exit code {returncode}", hint=hint
    )


_STDERR_INLINE_MAX_BYTES = 4096
_INLINE_PAYLOAD_WARN_BYTES = 1024


def _looks_like_base64_blob(token: str) -> bool:
    stripped = str(token or "").strip()
    if len(stripped) < 256:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r")
    if any(ch not in allowed for ch in stripped):
        return False
    return True


def _detect_inline_content_transfer_risk(command: str) -> tuple[str, str] | None:
    normalized = str(command or "")
    lowered = normalized.lower()

    if "<<" in normalized or "\n" in normalized:
        return (
            "inline_script",
            "Detected inline multi-line shell content. For small scripts, prefer "
            "'inspire bridge exec --stdin -- bash -s'. For local scripts, prefer "
            "'inspire bridge exec --file <path>'.",
        )

    tokens = normalized.split()
    if any(_looks_like_base64_blob(token) for token in tokens):
        return (
            "base64_blob",
            "Detected a large inline payload that looks like content transfer. Prefer "
            "'inspire bridge scp' for files, or 'inspire bridge exec --stdin -- bash -s' "
            "for small generated scripts.",
        )

    if len(normalized.encode("utf-8")) > _INLINE_PAYLOAD_WARN_BYTES:
        return (
            "oversized_inline_command",
            "Detected a large inline command payload. If this is script or file content, "
            "prefer 'inspire bridge exec --stdin -- bash -s' for small streamed scripts or "
            "'inspire bridge exec --file <path>' for local scripts.",
        )

    if ("cat " in lowered or "python " in lowered) and any(
        marker in lowered
        for marker in ("base64", ">>", "> /tmp", "> /workspace", ">/tmp", ">/workspace")
    ):
        return (
            "content_transfer_like_command",
            "This command looks like content transfer over 'bridge exec'. Prefer "
            "'inspire bridge scp' for local files, and keep 'bridge exec' focused on "
            "remote command execution.",
        )

    return None


def _maybe_warn_inline_content_transfer(ctx: Context, *, command: str, stdin_mode: bool) -> None:
    if stdin_mode:
        return
    detected = _detect_inline_content_transfer_risk(command)
    if detected is None:
        return
    _kind, message = detected
    emit_warning(ctx, message)


def _build_jupyter_stdin_command(command: str, stdin_content: str) -> str:
    encoded = base64.b64encode(stdin_content.encode("utf-8")).decode("ascii")
    return f"printf %s {shlex.quote(encoded)} | base64 -d | ({command})"


def _format_stderr_for_error(stderr: str) -> tuple[str, str | None]:
    """Format stderr for error output with a 4KB inline threshold.

    Short stderr (<= 4KB) is appended inline to the error message.
    Long stderr is written to a temp file and the path is referenced.

    Returns:
        (message_suffix, file_path): message_suffix is the text to append to
        the error message. file_path is the temp file path if stderr was
        written to a file, otherwise None.
    """
    if not stderr:
        return ("", None)

    stderr_stripped = stderr.strip()
    if not stderr_stripped:
        return ("", None)
    if len(stderr_stripped.encode("utf-8")) <= _STDERR_INLINE_MAX_BYTES:
        return (f"\n{stderr_stripped}", None)

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    fd, path = tempfile.mkstemp(
        suffix=f"-{timestamp}.log",
        prefix="inspire-stderr-",
        dir="/tmp",
    )
    _STDERR_TEMP_FILES.add(path)
    with os.fdopen(fd, "w") as f:
        f.write(stderr)
    return (
        f"\nFull stderr written to: {path} (auto-deleted on exit)",
        path,
    )


def try_exec_via_jupyter(
    ctx: Context,
    *,
    command: str,
    bridge_profile: BridgeProfile,
    runtime: EffectiveRemoteRuntime,
    timeout_s: int,
    log_path: str | None,
    exec_file_stage: ExecFileStage | None,
    detach: bool,
) -> int:
    """Execute a Bridge command through its notebook Jupyter terminal."""
    notebook_id = str(bridge_profile.notebook_id or "").strip()
    if not notebook_id:
        return _emit_error(
            ctx,
            "ConfigError",
            f"Bridge '{bridge_profile.name}' has no notebook_id binding for Jupyter transport.",
            hint="Re-create the bridge with 'inspire notebook ssh <id> --save-as <name>'.",
        )

    session = require_web_session(
        ctx,
        hint=(
            "Jupyter transport requires web authentication. "
            "Configure the selected Account Catalog entry and retry."
        ),
    )
    try:
        if exec_file_stage is not None:
            browser_api_module.upload_path_to_notebook(
                notebook_id=notebook_id,
                session=session,
                local_path=exec_file_stage.local_path,
                remote_path=exec_file_stage.scp_remote_path,
                timeout=timeout_s,
            )

        full_command = _build_remote_command(
            command=command,
            runtime=runtime,
            log_path=None if detach else log_path,
        )
        if detach:
            full_command = _build_detached_command(command=full_command, log_path=log_path)

        result = browser_api_module.execute_notebook_shell_command(
            notebook_id=notebook_id,
            command=full_command,
            session=session,
            timeout=timeout_s,
        )
    except TimeoutError:
        return _emit_error(
            ctx,
            "Timeout",
            f"Jupyter command timed out after {timeout_s}s",
            EXIT_TIMEOUT,
        )
    except Exception as error:  # noqa: BLE001 - normalize platform/Jupyter failures
        return _emit_error(
            ctx,
            "JupyterExecutionError",
            f"Jupyter execution failed: {error}",
            hint="Ensure the notebook is RUNNING and JupyterLab is accessible.",
        )

    if result.returncode != 0:
        message = f"Command failed with exit code {result.returncode}"
        if result.stdout.strip():
            suffix, _ = _format_stderr_for_error(result.stdout)
            message = f"{message}{suffix}"
        return _emit_error(ctx, "CommandFailed", message)

    if ctx.json_output:
        payload: dict[str, object] = {
            "status": "success",
            "method": "jupyter",
            "bridge": bridge_profile.name,
            "notebook_id": notebook_id,
            "returncode": result.returncode,
            # See the note in `notebook exec`: `job exec` names this `exit_code`, and reading the absent
            # key yields None, which a careful caller cannot distinguish from "the channel died".
            "exit_code": result.returncode,
            "stdout": result.stdout,
            # Null on this transport, never "": the Jupyter terminal is one merged PTY stream, so an empty
            # stderr here would read as "the command wrote nothing to stderr" (issue #51).
            "stderr": result.stderr,
            "stream_model": "pty-merged",
            "duration_ms": result.duration_ms,
        }
        if log_path is not None:
            payload["log_path"] = log_path
        emit_success(ctx, payload=payload)
        return EXIT_SUCCESS

    if result.stdout:
        click.echo(result.stdout, nl=not result.stdout.endswith("\n"))
    if log_path is not None:
        click.echo(f"Log saved to: {log_path}")
    return EXIT_SUCCESS


def try_exec_via_ssh_tunnel(
    ctx: Context,
    *,
    command: str,
    bridge_name: Optional[str],
    stdin_mode: bool,
    config: Config,
    runtime: EffectiveRemoteRuntime,
    timeout_s: int,
    is_tunnel_available_fn: Callable[..., bool],
    run_ssh_command_fn: Callable[..., object],
    run_ssh_command_streaming_fn: Callable[..., int],
    run_ssh_command_detached_fn: Callable[..., object] = run_ssh_command_detached,
    allocate_pty: bool = False,
    keepalive: bool = True,
    log_path: Optional[str] = None,
    exec_file_stage: ExecFileStage | None = None,
    detach: bool = False,
) -> Optional[int]:
    """Attempt the fast-path SSH tunnel execution.

    Returns:
        Exit code describing the final SSH execution outcome.
    """
    reconnect_limit = max(0, int(getattr(config, "tunnel_retries", 0)))
    reconnect_pause = float(getattr(config, "tunnel_retry_pause", 0.0) or 0.0)
    reconnect_state = NotebookBridgeReconnectState(
        reconnect_limit=reconnect_limit,
        reconnect_pause=reconnect_pause,
    )
    resolved_bridge_name = bridge_name
    force_rebuild = False
    opened_once = False
    ssh_execution_started = False
    stream_output_emitted = False

    def _maybe_get_preflight_web_session() -> web_session_module.WebSession | None:
        if reconnect_state.web_session is not None:
            return reconnect_state.web_session
        try:
            reconnect_state.web_session = web_session_module.get_web_session()
        except (ValueError, ConfigError) as auth_error:
            logger.debug(
                "Skipping notebook status preflight bridge=%s due to unavailable web auth: %s",
                resolved_bridge_name or "(default)",
                auth_error,
            )
            return None
        return reconnect_state.web_session

    def _require_rebuild(
        bridge: BridgeProfile,
        tunnel_config: TunnelConfig,
        *,
        reason: str,
    ) -> Optional[int]:
        nonlocal force_rebuild

        if not str(bridge.notebook_id or "").strip():
            hint = (
                "Run 'inspire tunnel status' to troubleshoot. "
                "If needed, re-create the bridge via "
                "'inspire notebook ssh <notebook-id> --save-as <name>'."
            )
            return _emit_error(
                ctx,
                "TunnelError",
                "SSH tunnel not available. "
                f"Bridge '{bridge.name}' is not responding "
                "(notebook may be stopped).",
                hint=hint,
            )

        if reconnect_state.reconnect_attempt >= reconnect_limit:
            return _emit_error(
                ctx,
                "TunnelError",
                "SSH tunnel not available",
                hint=(
                    "Auto-rebuild retries exhausted. Run 'inspire tunnel status' and "
                    "retry 'inspire notebook ssh <notebook-id> --save-as <name>'."
                ),
            )

        notebook_id = str(bridge.notebook_id or "").strip()
        if notebook_id:
            try:
                if reconnect_state.web_session is None:
                    reconnect_state.web_session = require_web_session(
                        ctx,
                        hint=(
                            "Automatic tunnel rebuild needs web authentication. "
                            "Set [auth].username and configure password via INSPIRE_PASSWORD "
                            'or [accounts."<username>"].password.'
                        ),
                    )
                notebook_detail = browser_api_module.get_notebook_detail(
                    notebook_id=notebook_id,
                    session=reconnect_state.web_session,
                )
                notebook_status = str((notebook_detail or {}).get("status") or "").strip().upper()
                if notebook_status != _RUNNING_NOTEBOOK_STATUS:
                    rendered_status = notebook_status or "UNKNOWN"
                    return _emit_error(
                        ctx,
                        "TunnelError",
                        (
                            "SSH tunnel not available. "
                            f"Bridge '{bridge.name}' notebook '{notebook_id}' "
                            f"is {rendered_status}."
                        ),
                        hint=(
                            f"Start it with 'inspire notebook start {notebook_id}' "
                            "or wait until it reports RUNNING, then retry."
                        ),
                    )
            except Exception as status_error:  # noqa: BLE001
                logger.debug(
                    "Skipping notebook status preflight bridge=%s notebook_id=%s error=%s",
                    bridge.name,
                    notebook_id,
                    status_error,
                )

        if not ctx.json_output:
            click.echo(
                (
                    f"{reason} "
                    f"(attempt {reconnect_state.reconnect_attempt + 1}/{reconnect_limit})..."
                ),
                err=True,
            )

        result = attempt_notebook_bridge_rebuild(
            state=reconnect_state,
            bridge_name=bridge.name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session_loader=lambda: require_web_session(
                ctx,
                hint=(
                    "Automatic tunnel rebuild needs web authentication. "
                    "Set [auth].username and configure password via INSPIRE_PASSWORD "
                    'or [accounts."<username>"].password.'
                ),
            ),
            runtime_loader=resolve_ssh_runtime_config,
            rebuild_fn=rebuild_notebook_bridge_profile,
            key_loader=lambda _path: load_ssh_public_key_material(),
        )

        if result.status is NotebookBridgeReconnectStatus.REBUILT:
            force_rebuild = False
            return None

        if result.status is NotebookBridgeReconnectStatus.RETRY_LATER:
            if result.pause_seconds > 0:
                time.sleep(result.pause_seconds)
            return None

        # EXHAUSTED or unexpected status — rebuild failed.
        if isinstance(result.error, (ValueError, ConfigError)):
            return _emit_error(
                ctx,
                "TunnelError",
                f"Automatic tunnel rebuild failed: {result.error}",
                hint="Check credentials, SSH key, and notebook status, then retry.",
            )

        return _emit_error(
            ctx,
            "TunnelError",
            (
                f"Automatic tunnel rebuild failed: {result.error}"
                if result.error
                else "SSH tunnel not available"
            ),
            hint="Verify the notebook is RUNNING and retry.",
        )

    def _is_ssh_disconnect_code(returncode: int) -> bool:
        return should_attempt_ssh_reconnect(
            returncode,
            interactive=False,
            allow_non_interactive=True,
        )

    def _retry_after_disconnect_reason(
        *,
        returncode: int,
        tunnel_config: TunnelConfig,
        bridge_name_to_check: str,
        command_output_seen: bool = False,
    ) -> str:
        """Classify a disconnect-style exit code before any automatic retry.

        SSH uses exit code 255 both for transport failures and some command failures.
        To avoid re-running non-idempotent commands incorrectly, require a quick
        tunnel health probe to fail before attempting rebuild/retry, and require that
        no remote output has been observed yet: once the command has produced output,
        retrying would repeat its remote side effects and reprint output the user has
        already seen. The returned reason says whether a retry will happen and, when it
        will not, whether remote output or a still-healthy tunnel is the cause.
        """
        if not _is_ssh_disconnect_code(returncode):
            return _SSH_RETRY_REASON_NOT_DISCONNECT
        if command_output_seen:
            return _SSH_RETRY_REASON_OUTPUT_SEEN

        try:
            tunnel_still_ready = is_tunnel_available_fn(
                bridge_name=bridge_name_to_check,
                config=tunnel_config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            )
        except Exception as probe_error:  # noqa: BLE001
            logger.debug("Skipping auto-retry after SSH 255: tunnel probe failed: %s", probe_error)
            return _SSH_RETRY_REASON_TUNNEL_ALIVE

        if tunnel_still_ready:
            return _SSH_RETRY_REASON_TUNNEL_ALIVE
        return _SSH_RETRY_REASON_RETRY

    def _should_retry_after_disconnect_code(
        *,
        returncode: int,
        tunnel_config: TunnelConfig,
        bridge_name_to_check: str,
        command_output_seen: bool = False,
    ) -> bool:
        return (
            _retry_after_disconnect_reason(
                returncode=returncode,
                tunnel_config=tunnel_config,
                bridge_name_to_check=bridge_name_to_check,
                command_output_seen=command_output_seen,
            )
            == _SSH_RETRY_REASON_RETRY
        )

    full_command = _build_remote_command(
        command=command,
        runtime=runtime,
        log_path=None if detach else log_path,
    )
    if detach:
        full_command = _build_detached_command(command=full_command, log_path=log_path)

    while True:
        try:
            tunnel_config = load_tunnel_config()
            bridge = tunnel_config.get_bridge(resolved_bridge_name)
            if bridge_name and bridge is None:
                return _emit_error(
                    ctx,
                    "ConfigError",
                    f"Bridge '{bridge_name}' not found.",
                    hint="Run 'inspire tunnel list' to see available bridge profiles.",
                )
            if bridge is None:
                return _emit_error(
                    ctx,
                    "TunnelError",
                    "No bridge configured for SSH execution.",
                    hint="Use 'inspire notebook ssh <notebook-id>' or 'inspire tunnel add' first.",
                )

            resolved_bridge_name = bridge.name

            # ── Notebook status preflight ───────────────────────────────────────
            # Check notebook status early to fail fast when the notebook is
            # STOPPED / FAILED instead of waiting for an SSH timeout.
            notebook_id = str(getattr(bridge, "notebook_id", "") or "").strip()
            if notebook_id:
                try:
                    preflight_session = _maybe_get_preflight_web_session()
                    if preflight_session is not None:
                        notebook_detail = browser_api_module.get_notebook_detail(
                            notebook_id=notebook_id,
                            session=preflight_session,
                        )
                        notebook_status = (
                            str((notebook_detail or {}).get("status") or "").strip().upper()
                        )
                        if notebook_status != _RUNNING_NOTEBOOK_STATUS:
                            rendered_status = notebook_status or "UNKNOWN"
                            return _emit_error(
                                ctx,
                                "TunnelError",
                                (
                                    f"Bridge '{bridge.name}' notebook '{notebook_id}' "
                                    f"is {rendered_status}."
                                ),
                                hint=(
                                    f"Start it with 'inspire notebook start {notebook_id}' "
                                    "or wait until it reports RUNNING, then retry."
                                ),
                            )
                except (ConnectionError, TimeoutError, ValueError, RuntimeError) as status_error:
                    logger.debug(
                        "Notebook status preflight failed bridge=%s notebook_id=%s error=%s",
                        bridge.name,
                        notebook_id,
                        status_error,
                    )

            availability_retries = 0 if force_rebuild else int(config.tunnel_retries)
            availability_pause = 0.0 if force_rebuild else float(config.tunnel_retry_pause)
            tunnel_ready = is_tunnel_available_fn(
                bridge_name=resolved_bridge_name,
                config=tunnel_config,
                retries=availability_retries,
                retry_pause=availability_pause,
                progressive=not force_rebuild,
            )

            if force_rebuild or not tunnel_ready:
                reconnect_error = _require_rebuild(
                    bridge,
                    tunnel_config,
                    reason=(
                        "SSH connection dropped; rebuilding tunnel automatically"
                        if force_rebuild
                        else "Tunnel unavailable; rebuilding automatically"
                    ),
                )
                if reconnect_error is not None:
                    return reconnect_error
                continue

            if exec_file_stage is not None:
                stage_failure = _stage_exec_file(
                    runtime=runtime,
                    local_path=exec_file_stage.local_path,
                    remote_dir=exec_file_stage.remote_dir,
                    scp_remote_path=exec_file_stage.scp_remote_path,
                    bridge_name=resolved_bridge_name,
                    tunnel_config=tunnel_config,
                    timeout=timeout_s,
                    run_ssh_command_fn=run_ssh_command_fn,
                    keepalive=keepalive,
                )
                if stage_failure is not None:
                    if (
                        stage_failure.returncode is not None
                        and _should_retry_after_disconnect_code(
                            returncode=stage_failure.returncode,
                            tunnel_config=tunnel_config,
                            bridge_name_to_check=resolved_bridge_name,
                        )
                    ):
                        force_rebuild = True
                        continue
                    return _emit_error(ctx, "FileUploadFailed", stage_failure.message)

            if detach:
                if _verbose_output(ctx):
                    emit_info(
                        ctx,
                        f"Launching in background (--detach) on bridge: {resolved_bridge_name}",
                    )
                ssh_execution_started = True
                result = run_ssh_command_detached_fn(
                    command=full_command,
                    bridge_name=resolved_bridge_name,
                    timeout=timeout_s,
                    keepalive=keepalive,
                )
                returncode = getattr(result, "returncode", 1)
                if returncode == 0:
                    if ctx.json_output:
                        payload: dict[str, object] = {
                            "status": "success",
                            "method": "ssh_tunnel",
                            "bridge": resolved_bridge_name,
                            "detach": True,
                            "returncode": returncode,
                            "exit_code": returncode,
                        }
                        if log_path is not None:
                            payload["log_path"] = log_path
                        emit_success(ctx, payload=payload)
                    else:
                        if _verbose_output(ctx):
                            emit_info(ctx, "Command launched successfully.")
                        if log_path is not None:
                            click.echo(f"Log saved to: {log_path}")
                    return EXIT_SUCCESS
                return _emit_error(
                    ctx,
                    "CommandFailed",
                    f"Failed to launch command (exit {returncode})",
                )

            if ctx.json_output:
                ssh_execution_started = True
                run_kwargs: dict[str, object] = {
                    "command": full_command,
                    "bridge_name": resolved_bridge_name,
                    "timeout": timeout_s,
                    "capture_output": True,
                }
                if stdin_mode:
                    run_kwargs["pass_stdin"] = True
                run_kwargs["allocate_pty"] = allocate_pty
                run_kwargs["keepalive"] = keepalive
                result = run_ssh_command_fn(
                    **run_kwargs,
                )
                returncode = getattr(result, "returncode", 1)
                stdout = getattr(result, "stdout", "") or ""
                stderr = getattr(result, "stderr", "") or ""
                if returncode == 0:
                    payload = {
                        "status": "success",
                        "method": "ssh_tunnel",
                        "bridge": resolved_bridge_name,
                        "returncode": returncode,
                        "exit_code": returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                        "stream_model": "pty-merged" if allocate_pty else "separate",
                        "output": stdout + stderr,
                    }
                    if log_path is not None:
                        payload["log_path"] = log_path
                    emit_success(
                        ctx,
                        payload=payload,
                    )
                    return EXIT_SUCCESS

                retry_reason = _retry_after_disconnect_reason(
                    returncode=returncode,
                    tunnel_config=tunnel_config,
                    bridge_name_to_check=resolved_bridge_name,
                    command_output_seen=bool(stdout or stderr),
                )
                if retry_reason == _SSH_RETRY_REASON_RETRY:
                    force_rebuild = True
                    continue

                message = f"Command failed with exit code {returncode}"
                if stderr:
                    stderr_suffix, _ = _format_stderr_for_error(stderr)
                    message = f"{message}{stderr_suffix}"
                return _emit_error(
                    ctx,
                    "CommandFailed",
                    message,
                    hint=(
                        _SSH_DROP_AFTER_OUTPUT_HINT
                        if retry_reason == _SSH_RETRY_REASON_OUTPUT_SEEN
                        else None
                    ),
                )

            if _verbose_output(ctx) and not opened_once:
                emit_info(ctx, "Using SSH tunnel (fast path)")
                emit_info(ctx, f"Bridge: {resolved_bridge_name}")
                emit_info(ctx, f"Command: {command}")
                emit_info(ctx, f"Working dir: {runtime.cwd}")
                if stdin_mode:
                    emit_info(ctx, "Stdin: passthrough")
                emit_info(ctx, "--- Command Output ---")
                opened_once = True

            ssh_execution_started = True

            def _stream_output(chunk: str) -> None:
                # Mirrors the runner's default echo, but records that remote output
                # already reached the user so a 255 cannot trigger a silent re-run.
                nonlocal stream_output_emitted
                if chunk:
                    stream_output_emitted = True
                click.echo(chunk, nl=False)

            stream_kwargs: dict[str, object] = {
                "command": full_command,
                "bridge_name": resolved_bridge_name,
                "timeout": timeout_s,
                "output_callback": _stream_output,
            }
            if stdin_mode:
                stream_kwargs["pass_stdin"] = True
            stream_kwargs["allocate_pty"] = allocate_pty
            stream_kwargs["keepalive"] = keepalive
            exit_code = run_ssh_command_streaming_fn(**stream_kwargs)
            if _verbose_output(ctx):
                emit_info(ctx, "--- End Output ---")

            if exit_code == 0:
                if log_path is not None:
                    click.echo(f"Log saved to: {log_path}")
                return EXIT_SUCCESS

            retry_reason = _retry_after_disconnect_reason(
                returncode=exit_code,
                tunnel_config=tunnel_config,
                bridge_name_to_check=resolved_bridge_name,
                command_output_seen=stream_output_emitted,
            )
            if retry_reason == _SSH_RETRY_REASON_RETRY:
                force_rebuild = True
                continue

            return _emit_command_failed(
                ctx,
                returncode=exit_code,
                hint=(
                    _SSH_DROP_AFTER_OUTPUT_HINT
                    if retry_reason == _SSH_RETRY_REASON_OUTPUT_SEEN
                    else None
                ),
            )

        except TunnelNotAvailableError as e:
            if ssh_execution_started:
                return _emit_error(
                    ctx,
                    "TunnelError",
                    f"SSH execution failed: {e}",
                )
            force_rebuild = True
            continue
        except subprocess.TimeoutExpired:
            emit_error(
                ctx,
                error_type="Timeout",
                message=f"Command timed out after {timeout_s}s",
                exit_code=EXIT_TIMEOUT,
            )
            return EXIT_TIMEOUT
        except Exception as e:
            if ssh_execution_started:
                return _emit_error(
                    ctx,
                    "SSHExecutionError",
                    f"SSH execution failed: {e}",
                )
            return _emit_error(
                ctx,
                "SSHExecutionError",
                f"SSH execution failed before command start: {e}",
            )


# ignore_unknown_options=True prevents Click from intercepting --flag arguments
# that belong to the remote command (e.g. `python train.py --configs x=1`).
# Without this, Click raises "no such option: --configs" before SSH is reached.
# Verified through real CLI testing: commands with --configs now pass through
# to the remote side instead of being rejected by the local parser.
@click.command("exec", context_settings=dict(ignore_unknown_options=True))
@click.argument("command_parts", nargs=-1, type=click.UNPROCESSED, required=False)
@click.option(
    "timeout",
    "--timeout",
    type=int,
    default=None,
    help="Timeout in seconds (default: config value)",
)
@click.option(
    "bridge",
    "--bridge",
    "-b",
    help="Bridge profile to use",
)
@click.option(
    "transport_name",
    "--transport",
    type=click.Choice(BRIDGE_TRANSPORTS, case_sensitive=False),
    default="auto",
    show_default=True,
    help="Execution transport: prefer healthy SSH, otherwise use notebook Jupyter",
)
@click.option(
    "stdin_mode",
    "--stdin",
    "--bash-stdin",
    is_flag=True,
    help="Pass local stdin to the remote command (auto when stdin is piped)",
)
@click.option(
    "path_prepend",
    "--path-prepend",
    multiple=True,
    help="Prepend a path to remote PATH before execution. Repeatable.",
)
@click.option(
    "no_pool",
    "--no-pool",
    is_flag=True,
    help="Disable SSH connection pooling (use subprocess-based SSH instead)",
)
@click.option(
    "interactive",
    "--interactive",
    "-i",
    is_flag=True,
    help="Force PTY allocation for interactive commands (e.g., htop, vim)",
)
@click.option(
    "--keepalive/--no-keepalive",
    default=True,
    help="Send SSH keepalive probes to prevent connection timeout on long-running commands (default: enabled)",
)
@click.option(
    "detach",
    "--detach",
    is_flag=True,
    help="Launch command as a true background job on the bridge and return immediately. Stdout/stderr go to --log if set, else /dev/null.",
)
@click.option(
    "log_flag",
    "--log",
    is_flag=True,
    help="Save output to a timestamped log file",
)
@click.option(
    "explain",
    "--explain",
    is_flag=True,
    help="Show the resolved remote runtime and final wrapped command without executing it.",
)
@json_option
@pass_context
def exec_command(
    ctx: Context,
    command_parts: tuple[str, ...],
    timeout: Optional[int],
    bridge: Optional[str],
    transport_name: str,
    stdin_mode: bool,
    path_prepend: tuple[str, ...],
    no_pool: bool = False,
    interactive: bool = False,
    keepalive: bool = True,
    detach: bool = False,
    log_flag: bool = False,
    explain: bool = False,
    json_output: bool = False,
) -> None:
    """Execute a command on the Bridge runner.

    Uses a healthy SSH tunnel when available and falls back to the notebook's
    Jupyter terminal when the bridge has a notebook binding. Commands run with
    PTY disabled by default. Use --interactive to require SSH with a PTY. Use
    --detach to launch long-running jobs that survive transport disconnect.

    SSH keepalive probes are enabled by default (--keepalive) to prevent connection
    timeout on long-running commands. Use --no-keepalive to disable.

    COMMAND is the shell command to run on Bridge (in INSPIRE_TARGET_DIR).
    Use --file PATH to run a local shell script inside the same resolved
    remote runtime; command arguments after -- are passed to the script.
    Command output (stdout/stderr) is automatically displayed after completion.

    For agent-style usage, keep inline commands small. If you need to run a
    local script, prefer `inspire bridge exec --file scripts/task.sh`. To stream a
    small local script, prefer `inspire bridge exec --stdin -- bash -s`.
    If you need to transfer a local file or larger payload, prefer
    `inspire bridge scp`.

    \b
    Examples:
        inspire bridge exec "uv venv .venv"
        inspire bridge exec --path-prepend ~/.local/bin -- uv run python train.py
        inspire bridge exec --explain -- python scripts/train.py --help
        inspire bridge exec "pip install torch" --timeout 600
        inspire bridge exec --file scripts/probe_axis.sh -- --out results/probe
        inspire bridge exec --stdin -- bash -s < scripts/watch_eval.sh
        printf '%s\n' 'echo hello' | inspire bridge exec --stdin -- bash -s
        cat benchmark_commands.txt | inspire bridge exec
        printf '%s\n' 'echo "$HOME"' | inspire bridge exec
        inspire bridge scp scripts/bootstrap.sh /workspace/bootstrap.sh
        inspire bridge exec --detach -- python train.py
        inspire bridge exec "hostname" --bridge qz-bridge
        inspire bridge exec --transport jupyter -b gpu-main -- nvidia-smi
        inspire bridge exec --interactive -- htop  # Interactive (with PTY)
    """

    json_output = resolve_json_output(ctx, json_output)

    # Auto-detect stdin-as-command mode when stdin is piped and no command
    # arguments are provided. This avoids local shell variable expansion.
    effective_script_file: str | None = None
    stdin_consumed_as_command = False
    stdin_staged_as_file = False
    if command_parts[:1] == ("--file",):
        if len(command_parts) < 2:
            raise click.UsageError("--file requires a local script path.")
        effective_script_file = command_parts[1]
        command_parts = tuple(command_parts[2:])

    if effective_script_file is None and not command_parts and _should_auto_passthrough_stdin():
        stdin_content = sys.stdin.read()
        if stdin_content.strip():
            if "\n" in stdin_content:
                fd, temp_path = tempfile.mkstemp(prefix="inspire-exec-stdin-", suffix=".sh")
                with os.fdopen(fd, "w") as f:
                    f.write(stdin_content)
                _STDIN_TEMP_FILES.add(temp_path)
                effective_script_file = temp_path
                stdin_staged_as_file = True
            else:
                command_parts = (stdin_content.strip(),)
                stdin_consumed_as_command = True

    if effective_script_file is not None and stdin_mode:
        raise click.UsageError("--file cannot be combined with --stdin.")

    local_script_path: Path | None = None
    remote_script_path: str | None = None
    remote_script_dir: str | None = None
    exec_file_stage: ExecFileStage | None = None
    command_source: str
    if effective_script_file is not None:
        local_script_path = _resolve_exec_file(ctx, effective_script_file)
        command = _build_exec_file_command(remote_path="<staged-script>", script_args=command_parts)
        user_command = f"--file {effective_script_file}"
        if command_parts:
            user_command = f"{user_command} -- {shlex.join(command_parts)}"
        command_source = "stdin" if stdin_staged_as_file else "file"
    else:
        command = _normalize_exec_command(command_parts)
        user_command = command
        command_source = "stdin" if stdin_consumed_as_command else "argv"

    try:
        config, _ = Config.from_files_and_env(require_target_dir=False, require_credentials=False)
    except ConfigError as e:
        emit_error(
            ctx,
            error_type="ConfigError",
            message=f"Configuration error: {e}",
            exit_code=EXIT_CONFIG_ERROR,
        )
        sys.exit(EXIT_CONFIG_ERROR)

    try:
        session_state = load_runtime_session_state_for_config(config)
        runtime = resolve_effective_remote_runtime(
            config,
            cli_path_prepend=path_prepend,
            session_state=session_state,
            require_cwd=True,
        )
    except ConfigError as e:
        emit_error(
            ctx,
            error_type="ConfigError",
            message=f"Configuration error: {e}",
            exit_code=EXIT_CONFIG_ERROR,
        )
        sys.exit(EXIT_CONFIG_ERROR)

    action_timeout = int(timeout) if timeout is not None else int(config.bridge_action_timeout)

    if local_script_path is not None:
        remote_script_dir, remote_script_path, remote_script_scp_path = (
            _build_exec_file_remote_paths(
                runtime=runtime,
                local_path=local_script_path,
            )
        )
        command = _build_exec_file_command(
            remote_path=remote_script_path,
            script_args=command_parts,
        )
        exec_file_stage = ExecFileStage(
            local_path=local_script_path,
            remote_dir=remote_script_dir,
            remote_path=remote_script_path,
            scp_remote_path=remote_script_scp_path,
        )

    effective_stdin_mode = (
        False
        if local_script_path is not None or stdin_consumed_as_command
        else stdin_mode or _should_auto_passthrough_stdin()
    )
    if local_script_path is None and command_source != "stdin":
        _maybe_warn_inline_content_transfer(
            ctx,
            command=command,
            stdin_mode=effective_stdin_mode,
        )

    log_path: Optional[str] = _build_log_path(runtime, user_command) if log_flag else None
    execution_command = _build_remote_command(
        command=command,
        runtime=runtime,
        log_path=None if detach else log_path,
    )
    if detach:
        execution_command = _build_detached_command(command=execution_command, log_path=log_path)

    if explain:
        _render_exec_explain(
            ctx=ctx,
            bridge=bridge,
            transport=transport_name,
            stdin_mode=effective_stdin_mode,
            runtime=runtime,
            full_command=execution_command,
            user_command=user_command,
            command_source=command_source,
            local_file=str(local_script_path) if local_script_path is not None else None,
            remote_file=remote_script_path,
        )
        sys.exit(EXIT_SUCCESS)

    tunnel_config = load_tunnel_config()
    try:
        transport_selection = select_bridge_transport(
            requested=transport_name,
            bridge_name=bridge,
            tunnel_config=tunnel_config,
            ssh_probe=is_tunnel_available,
        )
    except (ValueError, TunnelNotAvailableError) as error:
        message = str(error)
        if message == "No bridge configured.":
            message = "No bridge configured for SSH execution."
        emit_error(
            ctx,
            error_type="TunnelError",
            message=message,
            exit_code=EXIT_GENERAL_ERROR,
        )
        sys.exit(EXIT_GENERAL_ERROR)
    except Exception as error:  # BridgeNotFoundError derives from TunnelError
        message = str(error)
        if message == "No bridge configured.":
            message = "No bridge configured for SSH execution."
        emit_error(
            ctx,
            error_type="TunnelError",
            message=message,
            exit_code=EXIT_GENERAL_ERROR,
        )
        sys.exit(EXIT_GENERAL_ERROR)

    selected_transport = transport_selection.selected
    if selected_transport == "jupyter" and interactive:
        raise click.UsageError(
            "--interactive requires SSH transport; use --transport ssh or run a non-interactive command."
        )
    if selected_transport == "jupyter" and effective_stdin_mode:
        stdin_content = sys.stdin.read()
        command = _build_jupyter_stdin_command(command, stdin_content)

    if selected_transport == "jupyter":
        exit_code = try_exec_via_jupyter(
            ctx,
            command=command,
            bridge_profile=transport_selection.bridge,
            runtime=runtime,
            timeout_s=action_timeout,
            log_path=log_path,
            exec_file_stage=exec_file_stage,
            detach=detach,
        )
        sys.exit(exit_code)

    # Select pooled or regular SSH functions
    if not no_pool and is_paramiko_available():
        if _verbose_output(ctx):
            emit_info(ctx, "Using connection pool for SSH")
        ssh_cmd_fn = run_ssh_command_pooled
        ssh_stream_fn = run_ssh_command_streaming_pooled
    else:
        ssh_cmd_fn = run_ssh_command
        ssh_stream_fn = run_ssh_command_streaming

    try:
        ssh_exit_code = try_exec_via_ssh_tunnel(
            ctx,
            command=command,
            bridge_name=bridge,
            stdin_mode=effective_stdin_mode,
            config=config,
            runtime=runtime,
            timeout_s=action_timeout,
            is_tunnel_available_fn=is_tunnel_available,
            run_ssh_command_fn=ssh_cmd_fn,
            run_ssh_command_streaming_fn=ssh_stream_fn,
            run_ssh_command_detached_fn=run_ssh_command_detached,
            allocate_pty=interactive,
            keepalive=keepalive,
            log_path=log_path,
            exec_file_stage=exec_file_stage,
            detach=detach,
        )
        sys.exit(ssh_exit_code if ssh_exit_code is not None else EXIT_GENERAL_ERROR)
    except Exception:
        # Pool will be cleaned up by idle timeout
        raise


#: `--file` cannot be a Click option: everything after the remote command is forwarded
#: verbatim (`inspire bridge exec python train.py --file config.yaml` must reach the remote
#: process with its own `--file`), so the flag is peeled off the leading arguments by hand.
#: That kept it functional but invisible - the option table and `inspire schema` params both
#: omit what Click never parsed. Declaring it here puts it back into the generated contract,
#: with the position where it is actually consumed.
FRONT_OPTIONS = (
    {
        "name": "--file",
        "metavar": "PATH",
        "help": (
            "Run a local shell script inside the same resolved remote runtime; "
            "script arguments follow --."
        ),
    },
)

exec_command.front_options = FRONT_OPTIONS  # type: ignore[attr-defined]
