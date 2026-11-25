"""Orchestration: VSCode proxy, readiness fallback, full setup_notebook_rtunnel flow."""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any, Optional

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        pass


from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.core import (
    PLAYWRIGHT_INSTALL_HINT,
    _get_base_url,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.platform.web.browser_api.rtunnel.http_lab_resolution import (
    resolve_lab_url_http,
)
from inspire.platform.web.browser_api.rtunnel.transport_factory import create_transport
from inspire.platform.web.session import WebSession, get_web_session
from inspire.platform.web.session.auth import _get_auth_method
from inspire.platform.web.session.requests import build_requests_session

import requests

from ._jupyter import _extract_jupyter_token, _jupyter_server_base
from .commands import (
    SETUP_DONE_MARKER,
    SSHD_MISSING_MARKER,
    SSH_SERVER_MISSING_MARKER,
    STEP_FAILED_PREFIX,
    build_rtunnel_setup_commands,
    describe_rtunnel_setup_plan,
)
from .logging import (
    attach_failure_summary,
    bind_trace,
    clear_last_failure_summary,
    create_trace,
    format_trace_summary,
    _redact_text,
    set_last_failure_summary,
    trace_event,
    update_trace_summary,
)
from .probe import probe_existing_rtunnel_proxy_url
from .state import save_rtunnel_proxy_state
from .terminal import (
    _attach_ws_output_listener,
    _build_batch_setup_script,
    _build_terminal_websocket_url,
    _delete_terminal_via_api,
    _detach_ws_output_listener,
    _focus_terminal_input,
    _open_or_create_terminal,
    _send_setup_command_via_terminal_ws,
    _wait_for_terminal_surface,
    _wait_for_ws_capture,
)
from .upload import _resolve_rtunnel_binary
from .transports import TerminalTransport
from .verify import probe_rtunnel_proxy_once, redact_proxy_url

import logging

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel")

_SETUP_OUTPUT_TAIL_CHARS = 6000
_SETUP_TRACE_TAIL_LINES = 40
_SETUP_TRACE_TAIL_CHARS = 4000
_SETUP_ERR_CHARS = 2000
_SETUP_MARKER_PLACEHOLDER = f"{STEP_FAILED_PREFIX}<omitted>"


def _extract_setup_errors(output: str) -> list[str]:
    """Return setup error markers captured from remote terminal output."""
    errors: list[str] = []
    text = str(output or "")
    for marker in [SSHD_MISSING_MARKER, SSH_SERVER_MISSING_MARKER]:
        if marker in text and marker not in errors:
            errors.append(marker)
    for line in text.splitlines():
        item = line.strip()
        if item.startswith(STEP_FAILED_PREFIX) and item not in errors:
            errors.append(item)
    return errors


def _decode_setup_step_failure(marker: str) -> str | None:
    """Convert ``INSPIRE_STEP_FAILED=...`` into a readable diagnostic."""
    import base64
    import binascii

    if not str(marker or "").startswith(STEP_FAILED_PREFIX):
        return None
    payload = marker.split("=", 1)[1].strip()
    if not payload:
        return "Remote setup step failed."

    parts = payload.split("|")
    step_id = parts[0] or "unknown"
    fields: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value

    message = f"Remote setup step '{step_id}' failed"
    line = fields.get("line")
    if line:
        message += f" at line {line}"
    rc = fields.get("rc")
    if rc:
        message += f" (rc={rc})"

    def _decode_b64_field(name: str) -> str:
        encoded = fields.get(name, "")
        if not encoded:
            return ""
        try:
            return base64.b64decode(encoded).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return encoded

    err = _format_setup_output_excerpt(_decode_b64_field("err"), max_chars=_SETUP_ERR_CHARS)
    if err:
        message += f": {err}"

    cmd = _format_setup_output_excerpt(_decode_b64_field("cmd"), max_chars=1000)
    if cmd:
        message += f"\nRemote command: {cmd}"

    trace = _format_setup_trace_excerpt(_decode_b64_field("trace"))
    if trace:
        message += f"\nRemote trace tail:\n{trace}"
    return message


def _format_setup_errors(errors: list[str]) -> list[str]:
    formatted: list[str] = []
    for error in errors:
        formatted.append(_decode_setup_step_failure(error) or error)
    return formatted


def _first_setup_step_failure(errors: list[str]) -> str | None:
    for error in errors:
        message = _decode_setup_step_failure(error)
        if message:
            return message
    return None


def _format_setup_output_excerpt(text: str, *, max_chars: int = _SETUP_OUTPUT_TAIL_CHARS) -> str:
    value = _redact_text(_strip_encoded_setup_markers(str(text or ""))).strip()
    if not value:
        return ""
    # Jupyter terminals may echo the base64-encoded setup command before the
    # script's structured progress markers. It is noisy, can contain public-key
    # material, and provides no diagnostic value once marker output exists.
    marker_offset = value.find("INSPIRE_STEP_START=")
    if marker_offset >= 0:
        value = value[marker_offset:]
    value = "\n".join(line.rstrip() for line in value.splitlines()).strip()
    if len(value) > max_chars:
        value = f"...{value[-max_chars:]}"
    return value


def _strip_encoded_setup_markers(text: str) -> str:
    lines: list[str] = []
    for line in str(text or "").splitlines():
        if line.strip().startswith(STEP_FAILED_PREFIX):
            lines.append(_SETUP_MARKER_PLACEHOLDER)
        else:
            lines.append(line)
    return "\n".join(lines)


def _format_setup_trace_excerpt(text: str) -> str:
    value = _format_setup_output_excerpt(text, max_chars=_SETUP_TRACE_TAIL_CHARS)
    if not value:
        return ""
    lines = value.splitlines()
    if len(lines) > _SETUP_TRACE_TAIL_LINES:
        lines = lines[-_SETUP_TRACE_TAIL_LINES:]
        lines.insert(0, "...")
    return "\n".join(lines)


def _extract_setup_output_tail(result: dict[str, Any]) -> str:
    diagnostics = result.get("diagnostics")
    candidates = [
        result.get("stdoutTail"),
        result.get("stdout"),
        diagnostics.get("stdoutTail") if isinstance(diagnostics, dict) else None,
        diagnostics.get("stdout") if isinstance(diagnostics, dict) else None,
    ]
    for candidate in candidates:
        excerpt = _format_setup_output_excerpt(str(candidate or ""))
        if excerpt:
            return excerpt
    return ""


def _timing_enabled() -> bool:
    value = os.environ.get("INSPIRE_RTUNNEL_TIMING", "")
    return value.strip().lower() in {"1", "true", "yes"}


class _StepTimer:
    """Lightweight per-step timing collector for the rtunnel setup flow.

    When *enabled* is ``False`` every method is a no-op (zero overhead).
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled
        self._steps: list[tuple[str, float]] = []  # (label, elapsed_s)
        self._last = time.monotonic() if enabled else 0.0

    def mark(self, label: str) -> float:
        """Record elapsed time since the previous mark.

        Returns the step duration in seconds (0.0 when disabled).
        """
        if not self._enabled:
            return 0.0

        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._steps.append((label, elapsed))
        _log.debug("  [timing] %s: %.3fs", label, elapsed)
        return elapsed

    def summary(self) -> None:
        """Print a visual summary table to stderr."""
        if not self._enabled or not self._steps:
            return

        total = sum(s for _, s in self._steps)
        if total <= 0:
            return

        max_label = max(len(label) for label, _ in self._steps)
        bar_width = 30

        lines = ["\n  ── rtunnel timing summary ──"]
        for label, elapsed in self._steps:
            pct = elapsed / total * 100
            bar_len = int(round(pct / 100 * bar_width))
            bar = "#" * bar_len
            lines.append(f"  {label:<{max_label}}  {elapsed:6.2f}s  {pct:5.1f}%  {bar}")
        lines.append(f"  {'TOTAL':<{max_label}}  {total:6.2f}s")
        _log.info("\n".join(lines))


def _derive_vscode_proxy_url(proxy_url: str) -> str | None:
    """Derive a VSCode proxy URL from a Jupyter proxy URL.

    Many platform deployments expose both:
      - /jupyter/<notebook>/<token>/proxy/<port>/
      - /vscode/<notebook>/<token>/proxy/<port>/

    The VSCode proxy is generally more reliable for WebSocket-based tunnels.
    """
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return None
    if "/vscode/" in proxy_url:
        return proxy_url
    if "/jupyter/" not in proxy_url:
        return None
    return proxy_url.replace("/jupyter/", "/vscode/", 1)


def _ensure_proxy_readiness_with_fallback(
    *,
    proxy_url: str,
    port: int,
    timeout: int,
    context: Any = None,
    page: Any = None,
    session: requests.Session | None = None,
) -> tuple[str, list[str]]:
    diagnostics: list[str] = []
    derived_vscode_url = _derive_vscode_proxy_url(proxy_url)
    candidates: list[tuple[str, str]] = []
    if derived_vscode_url and derived_vscode_url != proxy_url:
        candidates.append(("derived_vscode", derived_vscode_url))
    candidates.append(("primary", proxy_url))

    for probe_label, candidate_url in candidates:
        trace_event("proxy_probe_candidate", mode=probe_label, proxy_url=candidate_url)
        ready, summary = probe_rtunnel_proxy_once(
            proxy_url=candidate_url,
            context=context,
            session=session,
            request_timeout_ms=min(max(timeout, 1) * 1000, 5000),
        )
        if ready:
            update_trace_summary(proxy_probe_result=f"{probe_label}_ready")
            return candidate_url, diagnostics

        diagnostics.append(f"{probe_label}={summary}")
        trace_event("proxy_probe_failed", mode=probe_label, error=summary)

    diagnostic_text = " | ".join(diagnostics)
    update_trace_summary(proxy_probe_result="all_candidates_failed")
    if diagnostics and all("websocket 403 " in item.lower() for item in diagnostics):
        raise ValueError(
            "Notebook ingress denied rtunnel WebSocket access (HTTP 403). "
            "The SSH server inside the notebook may be healthy, but interactive SSH "
            "requires a platform proxy policy change. "
            f"Probe results: {diagnostic_text}"
        )
    raise ValueError(
        "No notebook proxy candidate accepted the rtunnel WebSocket upgrade. "
        f"Probe results: {diagnostic_text}"
    )


def _send_rtunnel_setup_script_via_transport(
    *,
    transport: TerminalTransport,
    batch_cmd: str,
    timer: "_StepTimer",
    setup_timeout: int = 120,
) -> tuple[bool, list[str]]:
    """Send setup script via a generic terminal transport (HTTP or Playwright WS)."""
    detected_errors: list[str] = []
    trace_event("terminal_setup_attempt", transport=type(transport).__name__)
    update_trace_summary(terminal_transport=type(transport).__name__)

    term_name = transport.create_terminal()
    if not term_name:
        trace_event("terminal_setup_create_failed")
        update_trace_summary(setup_confirmed="false")
        return False, []

    try:
        result = transport.send_command(
            batch_cmd,
            SETUP_DONE_MARKER,
            timeout=max(int(setup_timeout), 1),
        )
    except (RuntimeError, TimeoutError, ValueError):
        trace_event("terminal_setup_send_exception")
        update_trace_summary(setup_confirmed="false")
        return False, []

    # Check for error markers in stdout
    stdout = result.get("stdout", "")
    detected_errors.extend(_extract_setup_errors(stdout))
    output_tail = _extract_setup_output_tail(result)
    if output_tail:
        update_trace_summary(setup_output_tail=output_tail)

    # Also check explicit errors from transport
    for err in result.get("errors", []):
        if err not in detected_errors:
            detected_errors.append(err)

    setup_confirmed = result.get("ok", False) or result.get("markerFound", False)

    if detected_errors:
        update_trace_summary(setup_errors=",".join(_format_setup_errors(detected_errors)))
        return setup_confirmed, detected_errors

    if setup_confirmed:
        _log.debug("  Sent setup script via Jupyter terminal WebSocket.")
        timer.mark("open_terminal")
        timer.mark("focus_xterm")
        timer.mark("build_and_send_cmd")
        update_trace_summary(setup_confirmed="true")
        trace_event("terminal_setup_completed", transport=type(transport).__name__)
        return True, []

    # Check if command was at least dispatched
    ws_command_dispatched = bool(
        result.get("wsConnected") and result.get("commandSent") and result.get("stdoutReceived")
    )
    if ws_command_dispatched:
        trace_event(
            "terminal_setup_unconfirmed_continue",
            transport=type(transport).__name__,
            prompt_detected=result.get("promptDetected"),
            stdout_len=result.get("stdoutLen"),
            stdout_tail=output_tail,
            elapsed=result.get("elapsed_ms"),
        )
        _log.warning(
            "  WebSocket terminal command was sent but completion was not confirmed; "
            "continuing without browser replay."
        )
        update_trace_summary(setup_confirmed="false")
        return False, []

    update_trace_summary(setup_confirmed="false")
    return False, []


def _send_rtunnel_setup_script_playwright(
    *,
    context: Any,
    page: Any,
    lab_frame: Any,
    batch_cmd: str,
    timer: "_StepTimer",
) -> tuple[bool, list[str]]:
    """Playwright-specific terminal setup with browser fallback."""
    detected_errors: list[str] = []
    ws_diagnostics: dict[str, Any] = {}
    setup_confirmed = False
    trace_event("terminal_setup_attempt", transport="terminal_ws")
    update_trace_summary(terminal_transport="terminal_ws")
    try:
        setup_confirmed = _send_setup_command_via_terminal_ws(
            context=context,
            lab_frame=lab_frame,
            batch_cmd=batch_cmd,
            detected_errors=detected_errors,
            diagnostics_out=ws_diagnostics,
        )
    except (PlaywrightError, RuntimeError, TimeoutError, ValueError):
        setup_confirmed = False
        trace_event("terminal_setup_ws_exception")

    # Propagate error markers immediately — even if WS returned False
    # (marker was captured before timeout/close)
    if detected_errors:
        update_trace_summary(setup_errors=",".join(_format_setup_errors(detected_errors)))
        return setup_confirmed, detected_errors

    if setup_confirmed:
        _log.debug("  Sent setup script via Jupyter terminal WebSocket.")
        timer.mark("open_terminal")
        timer.mark("focus_xterm")
        timer.mark("build_and_send_cmd")
        update_trace_summary(setup_confirmed="true")
        trace_event("terminal_setup_completed", transport="terminal_ws")
        return True, []

    ws_command_dispatched = bool(
        ws_diagnostics.get("wsConnected")
        and ws_diagnostics.get("commandSent")
        and ws_diagnostics.get("stdoutReceived")
    )
    ws_output_tail = _format_setup_output_excerpt(str(ws_diagnostics.get("stdoutTail") or ""))
    if ws_output_tail:
        update_trace_summary(setup_output_tail=ws_output_tail)
    if ws_command_dispatched:
        trace_event(
            "terminal_setup_unconfirmed_continue",
            transport="terminal_ws",
            prompt_detected=ws_diagnostics.get("promptDetected"),
            stdout_len=ws_diagnostics.get("stdoutLen"),
            stdout_tail=ws_output_tail,
            elapsed=ws_diagnostics.get("elapsed"),
        )
        _log.warning(
            "  WebSocket terminal command was sent but completion was not confirmed; "
            "continuing without browser replay."
        )
        update_trace_summary(setup_confirmed="false")
        return False, []

    _log.info("  WebSocket terminal setup unavailable, using browser automation.")
    trace_event("terminal_setup_fallback", transport="browser_automation")
    update_trace_summary(terminal_transport="browser_automation")

    browser_term_name: str | None = None
    ws_listener_attached = False
    try:
        result, browser_term_name = _open_or_create_terminal(context, page, lab_frame)
        if not result:
            raise ValueError("Failed to open Jupyter terminal")
        timer.mark("open_terminal")

        if not _focus_terminal_input(lab_frame, page):
            page.wait_for_timeout(350)
            if not _wait_for_terminal_surface(lab_frame, timeout_ms=2000):
                raise ValueError("Failed to focus Jupyter terminal: xterm surface not ready")
            if not _focus_terminal_input(lab_frame, page):
                raise ValueError("Failed to focus Jupyter terminal input")
        timer.mark("focus_xterm")

        # Attach a read-only WS listener for stdout marker detection
        if browser_term_name:
            try:
                ws_url = _build_terminal_websocket_url(lab_frame.url, browser_term_name)
                ws_listener_attached = _attach_ws_output_listener(
                    lab_frame,
                    ws_url=ws_url,
                    completion_marker=SETUP_DONE_MARKER,
                    error_markers=[SSHD_MISSING_MARKER, SSH_SERVER_MISSING_MARKER],
                )
                if ws_listener_attached:
                    trace_event("terminal_ws_listener_attached", term_name=browser_term_name)
                    _log.debug("  Attached WS output listener for marker detection.")
            except (PlaywrightError, RuntimeError, TimeoutError, ValueError):
                ws_listener_attached = False
                trace_event("terminal_ws_listener_attach_failed", term_name=browser_term_name)

        _log.debug("  Executing setup script (%d chars) in notebook terminal...", len(batch_cmd))
        page.keyboard.insert_text(batch_cmd)
        page.keyboard.press("Enter")
        timer.mark("build_and_send_cmd")

        # If WS listener is attached, wait for markers via polling
        if ws_listener_attached:
            ws_state = _wait_for_ws_capture(lab_frame, page, timeout_ms=120000)
            trace_event(
                "terminal_ws_capture_result",
                marker_found=ws_state.get("markerFound"),
                errors=",".join(ws_state.get("errors", [])),
                ws_connected=ws_state.get("wsConnected"),
                stdout_received=ws_state.get("stdoutReceived"),
                stdout_len=ws_state.get("stdoutLen"),
                stdout_tail=ws_state.get("stdoutTail"),
                ws_close_code=ws_state.get("wsCloseCode"),
            )
            ws_capture_tail = _format_setup_output_excerpt(str(ws_state.get("stdoutTail") or ""))
            if ws_capture_tail:
                update_trace_summary(setup_output_tail=ws_capture_tail)
            ws_errors = ws_state.get("errors", [])
            if ws_errors:
                update_trace_summary(setup_errors=",".join(_format_setup_errors(ws_errors)))
                return True, ws_errors
            if ws_state.get("markerFound"):
                update_trace_summary(setup_confirmed="true")
                return True, []
            # WS listener timed out without markers — unconfirmed
            update_trace_summary(setup_confirmed="false")
            return False, []

        update_trace_summary(setup_confirmed="false")
        return False, []
    finally:
        if ws_listener_attached:
            _detach_ws_output_listener(lab_frame)
        if browser_term_name:
            try:
                _delete_terminal_via_api(
                    context, lab_url=lab_frame.url, term_name=browser_term_name
                )
            except Exception:
                pass


def _send_rtunnel_setup_script(
    *,
    transport: TerminalTransport | None = None,
    batch_cmd: str = "",
    timer: "_StepTimer | None" = None,
    context: Any = None,
    page: Any = None,
    lab_frame: Any = None,
    setup_timeout: int = 120,
) -> tuple[bool, list[str]]:
    """Dispatch to transport-based or Playwright-based setup script sender."""
    if transport is not None:
        assert timer is not None
        return _send_rtunnel_setup_script_via_transport(
            transport=transport,
            batch_cmd=batch_cmd,
            timer=timer,
            setup_timeout=setup_timeout,
        )
    assert timer is not None
    return _send_rtunnel_setup_script_playwright(
        context=context, page=page, lab_frame=lab_frame, batch_cmd=batch_cmd, timer=timer
    )


def _wait_for_setup_completion(
    *,
    page: Any,
    setup_confirmed: bool,
    timer: "_StepTimer",
) -> None:
    if not setup_confirmed:
        page.wait_for_timeout(3000)
    else:
        page.wait_for_timeout(500)
    timer.mark("wait_marker")


def _capture_terminal_debug_artifact(*, page: Any, timer: "_StepTimer") -> None:
    try:
        diagnostic_dir = tempfile.mkdtemp(prefix="inspire-notebook-terminal-")
        screenshot_path = os.path.join(diagnostic_dir, "terminal.png")
        page.screenshot(path=screenshot_path)
        if os.path.isfile(screenshot_path):
            update_trace_summary(screenshot_path=screenshot_path)
            trace_event("debug_artifact_saved", screenshot_path=screenshot_path)
    except (PlaywrightError, OSError, RuntimeError, TimeoutError, ValueError, TypeError):
        pass
    timer.mark("screenshot")


def _verify_and_cache_rtunnel_proxy(
    *,
    notebook_id: str,
    jupyter_proxy_url: str,
    port: int,
    ssh_port: int,
    timeout: int,
    context: Any = None,
    page: Any = None,
    session: requests.Session | None = None,
    account: str | None,
    timer: "_StepTimer",
) -> str:
    proxy_url, probe_diagnostics = _ensure_proxy_readiness_with_fallback(
        proxy_url=jupyter_proxy_url,
        port=port,
        timeout=timeout,
        context=context,
        page=page,
        session=session,
    )
    if probe_diagnostics:
        update_trace_summary(probe_diagnostics=" | ".join(probe_diagnostics))
    timer.mark("verify_proxy")
    update_trace_summary(proxy_url=redact_proxy_url(proxy_url))
    trace_event("proxy_ready_for_ssh", proxy_url=proxy_url)

    try:
        save_rtunnel_proxy_state(
            notebook_id=notebook_id,
            proxy_url=proxy_url,
            port=port,
            ssh_port=ssh_port,
            base_url=_get_base_url(),
            account=account,
        )
    except OSError:
        pass
    else:
        trace_event("proxy_state_saved")
    timer.mark("save_state")
    return proxy_url


def _setup_notebook_rtunnel_http(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    session: Optional[WebSession] = None,
    timeout: int = 120,
) -> str:
    """Pure HTTP implementation for setup_notebook_rtunnel."""
    timing = _timing_enabled()
    timer = _StepTimer(enabled=timing)

    if session is None:
        session = get_web_session()
    account = session.login_username
    timer.mark("session_init")
    trace = create_trace(
        notebook_id=notebook_id,
        account=account,
        port=port,
        ssh_port=ssh_port,
        headless=True,
    )
    clear_last_failure_summary()
    with bind_trace(trace):
        trace_event("setup_start", timeout=timeout)

        existing = probe_existing_rtunnel_proxy_url(
            notebook_id=notebook_id,
            port=port,
            session=session,
            account=account,
        )
        if existing:
            timer.mark("probe_existing")
            timer.summary()
            update_trace_summary(
                proxy_url=redact_proxy_url(existing),
                proxy_probe_result="fast_path_reuse",
            )
            trace_event("fast_path_hit", proxy_url=existing)
            _log.info("Using existing rtunnel connection (fast path).")
            return existing

        timer.mark("probe_existing")
        trace_event("fast_path_miss")
        _log.info("Setting up rtunnel tunnel via HTTP...")

        from inspire.platform.web.browser_api.playwright_notebooks import build_jupyter_proxy_url

        lab_url = resolve_lab_url_http(notebook_id, session)
        base_url = _jupyter_server_base(lab_url)
        token = _extract_jupyter_token(lab_url)
        jupyter_proxy_url = build_jupyter_proxy_url(lab_url, port=port)
        update_trace_summary(lab_resolution="http")
        trace_event("lab_opened", lab_url=lab_url)
        timer.mark("open_lab")
        update_trace_summary(proxy_url=redact_proxy_url(jupyter_proxy_url))
        trace_event("proxy_url_built", proxy_url=jupyter_proxy_url)
        timer.mark("build_proxy_url")

        http_session = build_requests_session(session, base_url)
        transport: TerminalTransport | None = None
        try:
            contents_api_filename = _resolve_rtunnel_binary(
                session=http_session,
                lab_url=lab_url,
                ssh_runtime=ssh_runtime,
                token=token,
            )
            _log.debug("contents_api_filename=%s", contents_api_filename)

            setup_plan = describe_rtunnel_setup_plan(
                ssh_runtime=ssh_runtime,
                contents_api_filename=contents_api_filename,
            )
            update_trace_summary(
                bootstrap_mode=setup_plan.get("bootstrap_mode"),
                rtunnel_source=setup_plan.get("rtunnel_source"),
                upload_policy=setup_plan.get("upload_policy"),
            )
            trace_event("setup_plan", **setup_plan)

            cmd_lines = build_rtunnel_setup_commands(
                port=port,
                ssh_port=ssh_port,
                ssh_public_key=ssh_public_key,
                ssh_runtime=ssh_runtime,
                contents_api_filename=contents_api_filename,
            )
            batch_cmd = _build_batch_setup_script(cmd_lines)
            _log.debug("Setup script length: %d chars, %d commands", len(batch_cmd), len(cmd_lines))
            trace_event(
                "setup_script_built",
                command_count=len(cmd_lines),
                batch_length=len(batch_cmd),
            )

            transport = create_transport(
                auth_method="http",
                session=session,
                lab_url=lab_url,
                token=token,
            )
            setup_confirmed, setup_errors = _send_rtunnel_setup_script(
                transport=transport,
                batch_cmd=batch_cmd,
                timer=timer,
                setup_timeout=timeout,
            )
            _log.debug("Setup confirmed: %s", setup_confirmed)
            update_trace_summary(setup_confirmed=setup_confirmed)
            setup_errors_display = _format_setup_errors(setup_errors)
            trace_event(
                "setup_script_result",
                setup_confirmed=setup_confirmed,
                setup_errors=",".join(setup_errors_display),
            )

            setup_step_failure = _first_setup_step_failure(setup_errors)
            if setup_step_failure:
                raise RuntimeError(setup_step_failure)
            if SSHD_MISSING_MARKER in setup_errors:
                raise RuntimeError(
                    "OpenSSH bootstrap finished, but no SSH server was installed on "
                    "the notebook."
                )
            if SSH_SERVER_MISSING_MARKER in setup_errors:
                strategy = str(setup_plan.get("bootstrap_strategy") or "")
                if strategy.startswith("dropbear"):
                    raise RuntimeError(
                        "Dropbear bootstrap completed, but no SSH server process is "
                        "running on the notebook."
                    )
                raise RuntimeError("Notebook setup finished, but no SSH server process is running.")

            if not setup_confirmed:
                time.sleep(3)
            else:
                time.sleep(0.5)
            timer.mark("wait_marker")
            trace_event("setup_wait_complete", setup_confirmed=setup_confirmed)

            proxy_url = _verify_and_cache_rtunnel_proxy(
                notebook_id=notebook_id,
                jupyter_proxy_url=jupyter_proxy_url,
                port=port,
                ssh_port=ssh_port,
                timeout=timeout,
                session=http_session,
                account=account,
                timer=timer,
            )
            trace_event("setup_complete", proxy_url=proxy_url)
            return proxy_url
        except Exception as exc:
            update_trace_summary(last_error=str(exc))
            trace_event("setup_failed", error=str(exc), error_type=type(exc).__name__)
            set_last_failure_summary(format_trace_summary(trace))
            summary_message = attach_failure_summary(str(exc), trace)
            raise RuntimeError(summary_message) from exc
        finally:
            if transport is not None:
                try:
                    transport.cleanup()
                except Exception:
                    pass
            http_session.close()
            timer.summary()


def _setup_notebook_rtunnel_playwright(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Playwright-based implementation for setup_notebook_rtunnel."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            f"{PLAYWRIGHT_INSTALL_HINT}\nOr switch to HTTP auth: export INSPIRE_AUTH_METHOD=http"
        ) from None

    from inspire.platform.web.browser_api.playwright_notebooks import (
        build_jupyter_proxy_url,
        open_notebook_lab,
    )

    timing = _timing_enabled()
    timer = _StepTimer(enabled=timing)

    if session is None:
        session = get_web_session()
    account = session.login_username
    timer.mark("session_init")
    trace = create_trace(
        notebook_id=notebook_id,
        account=account,
        port=port,
        ssh_port=ssh_port,
        headless=headless,
    )
    clear_last_failure_summary()
    with bind_trace(trace):
        trace_event("setup_start", timeout=timeout)

        existing = probe_existing_rtunnel_proxy_url(
            notebook_id=notebook_id,
            port=port,
            session=session,
            account=account,
        )
        if existing:
            timer.mark("probe_existing")
            timer.summary()
            update_trace_summary(
                proxy_url=redact_proxy_url(existing),
                proxy_probe_result="fast_path_reuse",
            )
            trace_event("fast_path_hit", proxy_url=existing)
            _log.info("Using existing rtunnel connection (fast path).")
            return existing

        timer.mark("probe_existing")
        trace_event("fast_path_miss")
        _log.info("Setting up rtunnel tunnel via browser automation...")

        with sync_playwright() as p:
            browser = None
            context = None
            lab_frame = None
            try:
                browser = _launch_browser(p, headless=headless)
                trace_event("playwright_launch", headless=headless)
                timer.mark("playwright_launch")
                context = _new_context(browser, storage_state=session.storage_state)
                page = context.new_page()
                timer.mark("context_and_page")

                lab_frame = open_notebook_lab(page, notebook_id=notebook_id, timeout=60000)
                update_trace_summary(lab_resolution="resolved")
                trace_event("lab_opened", lab_url=lab_frame.url)
                timer.mark("open_lab")
                jupyter_proxy_url = build_jupyter_proxy_url(lab_frame.url, port=port)
                update_trace_summary(proxy_url=redact_proxy_url(jupyter_proxy_url))
                trace_event("proxy_url_built", proxy_url=jupyter_proxy_url)
                timer.mark("build_proxy_url")

                try:
                    lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=30000)
                except (
                    PlaywrightError,
                    TimeoutError,
                    RuntimeError,
                    AttributeError,
                    ValueError,
                ):
                    trace_event("lab_spinner_wait_skipped")
                timer.mark("wait_spinner")

                contents_api_filename = _resolve_rtunnel_binary(
                    context=context,
                    lab_url=lab_frame.url,
                    ssh_runtime=ssh_runtime,
                )
                _log.debug("contents_api_filename=%s", contents_api_filename)

                setup_plan = describe_rtunnel_setup_plan(
                    ssh_runtime=ssh_runtime,
                    contents_api_filename=contents_api_filename,
                )
                update_trace_summary(
                    bootstrap_mode=setup_plan.get("bootstrap_mode"),
                    rtunnel_source=setup_plan.get("rtunnel_source"),
                    upload_policy=setup_plan.get("upload_policy"),
                )
                trace_event("setup_plan", **setup_plan)

                cmd_lines = build_rtunnel_setup_commands(
                    port=port,
                    ssh_port=ssh_port,
                    ssh_public_key=ssh_public_key,
                    ssh_runtime=ssh_runtime,
                    contents_api_filename=contents_api_filename,
                )
                batch_cmd = _build_batch_setup_script(cmd_lines)
                _log.debug(
                    "Setup script length: %d chars, %d commands", len(batch_cmd), len(cmd_lines)
                )
                trace_event(
                    "setup_script_built",
                    command_count=len(cmd_lines),
                    batch_length=len(batch_cmd),
                )
                setup_confirmed, setup_errors = _send_rtunnel_setup_script(
                    context=context,
                    page=page,
                    lab_frame=lab_frame,
                    batch_cmd=batch_cmd,
                    timer=timer,
                )
                _log.debug("Setup confirmed: %s", setup_confirmed)
                update_trace_summary(setup_confirmed=setup_confirmed)
                setup_errors_display = _format_setup_errors(setup_errors)
                trace_event(
                    "setup_script_result",
                    setup_confirmed=setup_confirmed,
                    setup_errors=",".join(setup_errors_display),
                )

                setup_step_failure = _first_setup_step_failure(setup_errors)
                if setup_step_failure:
                    raise RuntimeError(setup_step_failure)
                if SSHD_MISSING_MARKER in setup_errors:
                    raise RuntimeError(
                        "OpenSSH bootstrap finished, but no SSH server was installed on "
                        "the notebook."
                    )
                if SSH_SERVER_MISSING_MARKER in setup_errors:
                    strategy = str(setup_plan.get("bootstrap_strategy") or "")
                    if strategy.startswith("dropbear"):
                        raise RuntimeError(
                            "Dropbear bootstrap completed, but no SSH server process is "
                            "running on the notebook."
                        )
                    raise RuntimeError(
                        "Notebook setup finished, but no SSH server process is running."
                    )
                _wait_for_setup_completion(
                    page=page,
                    setup_confirmed=setup_confirmed,
                    timer=timer,
                )
                trace_event("setup_wait_complete", setup_confirmed=setup_confirmed)
                _capture_terminal_debug_artifact(page=page, timer=timer)
                proxy_url = _verify_and_cache_rtunnel_proxy(
                    notebook_id=notebook_id,
                    jupyter_proxy_url=jupyter_proxy_url,
                    port=port,
                    ssh_port=ssh_port,
                    timeout=timeout,
                    context=context,
                    page=page,
                    account=account,
                    timer=timer,
                )
                trace_event("setup_complete", proxy_url=proxy_url)
                return proxy_url
            except Exception as exc:
                update_trace_summary(last_error=str(exc))
                trace_event("setup_failed", error=str(exc), error_type=type(exc).__name__)
                set_last_failure_summary(format_trace_summary(trace))
                summary_message = attach_failure_summary(str(exc), trace)
                raise RuntimeError(summary_message) from exc
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                timer.summary()


def _setup_notebook_rtunnel_sync(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Sync implementation for setup_notebook_rtunnel."""
    auth_method = _get_auth_method()
    if auth_method == "http":
        return _setup_notebook_rtunnel_http(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            ssh_runtime=ssh_runtime,
            session=session,
            timeout=timeout,
        )
    return _setup_notebook_rtunnel_playwright(
        notebook_id=notebook_id,
        port=port,
        ssh_port=ssh_port,
        ssh_public_key=ssh_public_key,
        ssh_runtime=ssh_runtime,
        session=session,
        headless=headless,
        timeout=timeout,
    )


# ============================================================================
# Public entry point
# ============================================================================


def setup_notebook_rtunnel(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Ensure the notebook exposes an rtunnel server via Jupyter proxy."""
    if _in_asyncio_loop():
        return _run_in_thread(
            _setup_notebook_rtunnel_sync,
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            ssh_runtime=ssh_runtime,
            session=session,
            headless=headless,
            timeout=timeout,
        )
    return _setup_notebook_rtunnel_sync(
        notebook_id=notebook_id,
        port=port,
        ssh_port=ssh_port,
        ssh_public_key=ssh_public_key,
        ssh_runtime=ssh_runtime,
        session=session,
        headless=headless,
        timeout=timeout,
    )
