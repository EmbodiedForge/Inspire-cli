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

from .commands import SSHD_MISSING_MARKER, build_rtunnel_setup_commands
from .probe import probe_existing_rtunnel_proxy_url
from .state import save_rtunnel_proxy_state
from .terminal import (
    _build_batch_setup_script,
    _delete_terminal_via_api,
    _focus_terminal_input,
    _open_or_create_terminal,
    _send_setup_command_via_terminal_ws,
    _wait_for_terminal_surface,
)
from .upload import _resolve_rtunnel_binary
from .verify import redact_proxy_url, wait_for_rtunnel_reachable

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


def _build_vscode_proxy_url(page, *, port: int) -> str | None:  # noqa: ANN001
    from urllib.parse import parse_qs, urlparse

    vscode_url = None
    for frame in page.frames:
        if "/vscode/" in (frame.url or ""):
            vscode_url = frame.url
            break
    if not vscode_url:
        return None

    parsed = urlparse(vscode_url)
    token = parse_qs(parsed.query).get("token", [None])[0]
    base = vscode_url.split("?", 1)[0].rstrip("/")
    proxy_url = f"{base}/proxy/{port}/"
    if token:
        proxy_url = f"{proxy_url}?token={token}"
    return proxy_url


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


def _extract_probe_error_summary(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        return error.__class__.__name__

    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return error.__class__.__name__

    headline = lines[0]
    last_response = next((line for line in lines if line.startswith("Last response:")), "")
    if last_response:
        return f"{headline}; {last_response}"
    return headline


def _ensure_proxy_readiness_with_fallback(
    *,
    proxy_url: str,
    port: int,
    timeout: int,
    context,  # noqa: ANN001
    page,  # noqa: ANN001
) -> tuple[str, list[str]]:
    import sys as _sys

    diagnostics: list[str] = []
    primary_verify_timeout_s = max(12, min(timeout, 35))

    derived_vscode_url = _derive_vscode_proxy_url(proxy_url)
    if derived_vscode_url and derived_vscode_url != proxy_url:
        _sys.stderr.write(
            f"  Probing VSCode proxy URL first: {redact_proxy_url(derived_vscode_url)}\n"
        )
        _sys.stderr.flush()
        try:
            wait_for_rtunnel_reachable(
                proxy_url=derived_vscode_url,
                timeout_s=min(6, timeout),
                context=context,
                page=page,
            )
            return derived_vscode_url, diagnostics
        except (
            PlaywrightError,
            ConnectionError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as derived_error:
            diagnostics.append(f"derived={_extract_probe_error_summary(derived_error)}")

    try:
        wait_for_rtunnel_reachable(
            proxy_url=proxy_url,
            timeout_s=primary_verify_timeout_s,
            context=context,
            page=page,
        )
        return proxy_url, diagnostics
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ) as primary_error:
        diagnostics.append(f"primary={_extract_probe_error_summary(primary_error)}")

    fallback_proxy_url = _build_vscode_proxy_url(page, port=port)
    if not fallback_proxy_url:
        try:
            vscode_tab = page.locator('img[alt="vscode"]').first
            if vscode_tab.count() > 0:
                vscode_tab.click(timeout=1500)
                page.wait_for_timeout(200)
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass
        fallback_proxy_url = _build_vscode_proxy_url(page, port=port)

    best_for_ssh = proxy_url
    if fallback_proxy_url and fallback_proxy_url != proxy_url:
        best_for_ssh = fallback_proxy_url

    if not fallback_proxy_url or fallback_proxy_url == proxy_url:
        _sys.stderr.write("  Proxy did not pass HTTP readiness; continuing with SSH preflight.\n")
        _sys.stderr.flush()
        return best_for_ssh, diagnostics

    _sys.stderr.write(f"  Trying alternate proxy URL: {redact_proxy_url(fallback_proxy_url)}\n")
    _sys.stderr.flush()
    try:
        wait_for_rtunnel_reachable(
            proxy_url=fallback_proxy_url,
            timeout_s=max(12, min(timeout, 45)),
            context=context,
            page=page,
        )
        return fallback_proxy_url, diagnostics
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ) as fallback_error:
        diagnostics.append(f"fallback={_extract_probe_error_summary(fallback_error)}")
        _sys.stderr.write(
            "  Fallback proxy did not pass HTTP readiness; " "continuing with SSH preflight.\n"
        )
        _sys.stderr.flush()
        return best_for_ssh, diagnostics


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
    setup_sent_via_ws = False
    try:
        setup_sent_via_ws = _send_setup_command_via_terminal_ws(
            context=context,
            lab_frame=lab_frame,
            batch_cmd=batch_cmd,
            detected_errors=detected_errors,
        )
    except (PlaywrightError, RuntimeError, TimeoutError, ValueError):
        setup_sent_via_ws = False

    # Propagate error markers immediately — even if WS returned False
    # (marker was captured before timeout/close)
    if detected_errors:
        return setup_sent_via_ws, detected_errors

    if setup_sent_via_ws:
        _sys.stderr.write("  Sent setup script via Jupyter terminal WebSocket.\n")
        _sys.stderr.flush()
        timer.mark("open_terminal")
        timer.mark("focus_xterm")
        timer.mark("build_and_send_cmd")
        return True, []

    _sys.stderr.write("  WebSocket terminal setup unavailable, using browser automation.\n")
    _sys.stderr.flush()

    browser_term_name: str | None = None
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

        _sys.stderr.write(
            f"  Executing setup script ({len(batch_cmd)} chars) in notebook terminal...\n"
        )
        _sys.stderr.flush()
        page.keyboard.insert_text(batch_cmd)
        page.keyboard.press("Enter")
        timer.mark("build_and_send_cmd")
        return False, []
    finally:
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
    setup_sent_via_ws: bool,
    timer: "_StepTimer",
) -> None:
    if not setup_sent_via_ws:
        page.wait_for_timeout(3000)
    else:
        page.wait_for_timeout(500)
    timer.mark("wait_marker")


def _capture_terminal_debug_artifact(*, page: Any, timer: "_StepTimer") -> None:
    try:
        page.screenshot(path="/tmp/notebook_terminal_debug.png")
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
    import sys as _sys

    _sys.stderr.write(
        f"  Verifying rtunnel is reachable at: {redact_proxy_url(jupyter_proxy_url)}\n"
    )
    _sys.stderr.flush()
    proxy_url, probe_diagnostics = _ensure_proxy_readiness_with_fallback(
        proxy_url=jupyter_proxy_url,
        port=port,
        timeout=timeout,
        context=context,
        page=page,
    )
    if probe_diagnostics:
        _sys.stderr.write("  Proxy readiness summary: " + " | ".join(probe_diagnostics) + "\n")
        _sys.stderr.flush()
    timer.mark("verify_proxy")

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

    existing = probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=port,
        session=session,
        account=account,
    )
    if existing:
        timer.mark("probe_existing")
        timer.summary()
        _sys.stderr.write("Using existing rtunnel connection (fast path).\n")
        _sys.stderr.flush()
        return existing

    timer.mark("probe_existing")
    _sys.stderr.write("Setting up rtunnel tunnel via browser automation...\n")
    _sys.stderr.flush()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        timer.mark("playwright_launch")
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()
        timer.mark("context_and_page")

        try:
            lab_frame = open_notebook_lab(page, notebook_id=notebook_id, timeout=60000)
            timer.mark("open_lab")
            jupyter_proxy_url = build_jupyter_proxy_url(lab_frame.url, port=port)
            timer.mark("build_proxy_url")

            try:
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=30000)
            except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
                pass
            timer.mark("wait_spinner")

            contents_api_filename = _resolve_rtunnel_binary(
                context=context,
                lab_url=lab_frame.url,
                ssh_runtime=ssh_runtime,
            )

            _log.debug("contents_api_filename=%s", contents_api_filename)
            cmd_lines = build_rtunnel_setup_commands(
                port=port,
                ssh_port=ssh_port,
                ssh_public_key=ssh_public_key,
                ssh_runtime=ssh_runtime,
                contents_api_filename=contents_api_filename,
            )
            batch_cmd = _build_batch_setup_script(cmd_lines)
            _log.debug("Setup script length: %d chars, %d commands", len(batch_cmd), len(cmd_lines))
            setup_sent_via_ws, setup_errors = _send_rtunnel_setup_script(
                context=context,
                page=page,
                lab_frame=lab_frame,
                batch_cmd=batch_cmd,
                timer=timer,
            )
            _log.debug("Setup script sent via WS: %s", setup_sent_via_ws)

            if SSHD_MISSING_MARKER in setup_errors:
                raise RuntimeError(
                    "SSH server (sshd) could not be installed on the notebook.\n"
                    "Possible causes: no internet access for apt-get, or a\n"
                    "misconfigured sshd_deb_dir (bad path / empty directory).\n\n"
                    "Configure an APT mirror in your project config "
                    "(.inspire/config.toml):\n\n"
                    "  [ssh]\n"
                    '  apt_mirror_url = "http://your-internal-mirror/ubuntu"\n\n'
                    "Or provide pre-downloaded sshd .deb packages:\n\n"
                    "  [ssh]\n"
                    '  sshd_deb_dir = "/shared/path/to/sshd-debs"\n'
                )
            _wait_for_setup_completion(
                page=page,
                setup_sent_via_ws=setup_sent_via_ws,
                timer=timer,
            )
            _capture_terminal_debug_artifact(page=page, timer=timer)
            return _verify_and_cache_rtunnel_proxy(
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

        finally:
            timer.summary()


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
