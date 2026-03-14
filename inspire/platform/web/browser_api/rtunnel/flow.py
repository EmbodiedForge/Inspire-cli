"""Orchestration: VSCode proxy, readiness fallback, full setup_notebook_rtunnel flow."""

from __future__ import annotations

import os
import time
from typing import Any, Optional

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        pass


from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.core import (
    _get_base_url,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.platform.web.session import WebSession, get_web_session

from .commands import (
    SETUP_DONE_MARKER,
    SSHD_MISSING_MARKER,
    SSH_SERVER_MISSING_MARKER,
    build_rtunnel_setup_commands,
    describe_rtunnel_setup_plan,
)
from .diagnostics import collect_notebook_rtunnel_diagnostics
from .logging import (
    attach_failure_summary,
    bind_trace,
    clear_last_failure_summary,
    create_trace,
    format_trace_summary,
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
from .verify import probe_rtunnel_proxy_once, redact_proxy_url

import logging

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel")


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
        import sys as _sys

        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._steps.append((label, elapsed))
        _sys.stderr.write(f"  [timing] {label}: {elapsed:.3f}s\n")
        _sys.stderr.flush()
        return elapsed

    def summary(self) -> None:
        """Print a visual summary table to stderr."""
        if not self._enabled or not self._steps:
            return
        import sys as _sys

        total = sum(s for _, s in self._steps)
        if total <= 0:
            return

        max_label = max(len(label) for label, _ in self._steps)
        bar_width = 30

        _sys.stderr.write("\n  ── rtunnel timing summary ──\n")
        for label, elapsed in self._steps:
            pct = elapsed / total * 100
            bar_len = int(round(pct / 100 * bar_width))
            bar = "#" * bar_len
            _sys.stderr.write(f"  {label:<{max_label}}  {elapsed:6.2f}s  {pct:5.1f}%  {bar}\n")
        _sys.stderr.write(f"  {'TOTAL':<{max_label}}  {total:6.2f}s\n")
        _sys.stderr.flush()


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
    context,  # noqa: ANN001
    page,  # noqa: ANN001
) -> tuple[str, list[str]]:
    diagnostics: list[str] = []
    derived_vscode_url = _derive_vscode_proxy_url(proxy_url)
    preferred_proxy_url = proxy_url
    probe_label = "primary"

    if derived_vscode_url and derived_vscode_url != proxy_url:
        preferred_proxy_url = derived_vscode_url
        probe_label = "derived_vscode"

    trace_event("proxy_probe_candidate", mode=probe_label, proxy_url=preferred_proxy_url)
    ready, summary = probe_rtunnel_proxy_once(
        proxy_url=preferred_proxy_url,
        context=context,
        request_timeout_ms=min(max(timeout, 1) * 1000, 5000),
    )

    if ready:
        update_trace_summary(proxy_probe_result=f"{probe_label}_ready")
        return preferred_proxy_url, diagnostics

    diagnostics.append(f"{probe_label}={summary}")
    trace_event("proxy_probe_failed", mode=probe_label, error=summary)
    update_trace_summary(proxy_probe_result=f"{probe_label}_failed_continue_to_ssh")
    if preferred_proxy_url != proxy_url:
        return proxy_url, diagnostics
    return preferred_proxy_url, diagnostics


def _send_rtunnel_setup_script(
    *,
    context: Any,
    page: Any,
    lab_frame: Any,
    batch_cmd: str,
    timer: "_StepTimer",
) -> tuple[bool, list[str]]:
    import sys as _sys

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
        update_trace_summary(setup_errors=",".join(detected_errors))
        return setup_confirmed, detected_errors

    if setup_confirmed:
        _sys.stderr.write("  Sent setup script via Jupyter terminal WebSocket.\n")
        _sys.stderr.flush()
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
    if ws_command_dispatched:
        trace_event(
            "terminal_setup_unconfirmed_continue",
            transport="terminal_ws",
            prompt_detected=ws_diagnostics.get("promptDetected"),
            stdout_len=ws_diagnostics.get("stdoutLen"),
            elapsed=ws_diagnostics.get("elapsed"),
        )
        _sys.stderr.write(
            "  WebSocket terminal command was sent but completion was not confirmed; "
            "continuing without browser replay.\n"
        )
        _sys.stderr.flush()
        update_trace_summary(setup_confirmed="false")
        return False, []

    _sys.stderr.write("  WebSocket terminal setup unavailable, using browser automation.\n")
    _sys.stderr.flush()
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
                    _sys.stderr.write("  Attached WS output listener for marker detection.\n")
                    _sys.stderr.flush()
            except (PlaywrightError, RuntimeError, TimeoutError, ValueError):
                ws_listener_attached = False
                trace_event("terminal_ws_listener_attach_failed", term_name=browser_term_name)

        _sys.stderr.write(
            f"  Executing setup script ({len(batch_cmd)} chars) in notebook terminal...\n"
        )
        _sys.stderr.flush()
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
                ws_close_code=ws_state.get("wsCloseCode"),
            )
            ws_errors = ws_state.get("errors", [])
            if ws_errors:
                update_trace_summary(setup_errors=",".join(ws_errors))
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
    screenshot_path = "/tmp/notebook_terminal_debug.png"
    try:
        page.screenshot(path=screenshot_path)
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
    context: Any,
    page: Any,
    account: str | None,
    timer: "_StepTimer",
) -> str:
    proxy_url, probe_diagnostics = _ensure_proxy_readiness_with_fallback(
        proxy_url=jupyter_proxy_url,
        port=port,
        timeout=timeout,
        context=context,
        page=page,
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
    import sys as _sys

    from playwright.sync_api import sync_playwright

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
            _sys.stderr.write("Using existing rtunnel connection (fast path).\n")
            _sys.stderr.flush()
            return existing

        timer.mark("probe_existing")
        trace_event("fast_path_miss")
        _sys.stderr.write("Setting up rtunnel tunnel via browser automation...\n")
        _sys.stderr.flush()

        try:
            with sync_playwright() as p:
                browser = _launch_browser(p, headless=headless)
                trace_event("playwright_launch", headless=headless)
                timer.mark("playwright_launch")
                context = _new_context(browser, storage_state=session.storage_state)
                page = context.new_page()
                timer.mark("context_and_page")

                try:
                    lab_frame = open_notebook_lab(page, notebook_id=notebook_id, timeout=60000)
                    update_trace_summary(lab_resolution="resolved")
                    trace_event("lab_opened", lab_url=lab_frame.url)
                    timer.mark("open_lab")
                    jupyter_proxy_url = build_jupyter_proxy_url(lab_frame.url, port=port)
                    update_trace_summary(proxy_url=redact_proxy_url(jupyter_proxy_url))
                    trace_event("proxy_url_built", proxy_url=jupyter_proxy_url)
                    timer.mark("build_proxy_url")

                    try:
                        lab_frame.locator("text=加载中").first.wait_for(
                            state="hidden", timeout=30000
                        )
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
                    trace_event(
                        "setup_script_result",
                        setup_confirmed=setup_confirmed,
                        setup_errors=",".join(setup_errors),
                    )

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
                finally:
                    timer.summary()
        except Exception as exc:
            doctor = collect_notebook_rtunnel_diagnostics(
                notebook_id=notebook_id,
                port=port,
                ssh_port=ssh_port,
                ssh_runtime=ssh_runtime,
                session=session,
                headless=headless,
            )
            if doctor is not None:
                update_trace_summary(
                    diagnosis_observed=doctor.observed,
                    diagnosis_excerpt=doctor.excerpt,
                )
            update_trace_summary(last_error=str(exc))
            trace_event("setup_failed", error=str(exc), error_type=type(exc).__name__)
            set_last_failure_summary(format_trace_summary(trace))
            summary_message = attach_failure_summary(str(exc), trace)
            raise RuntimeError(summary_message) from exc


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
