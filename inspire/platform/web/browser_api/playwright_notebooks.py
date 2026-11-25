"""Playwright-based notebook automation (exec + Jupyter navigation).

Phase 3: The primary ``run_command_in_notebook`` path now uses pure HTTP
WebSocket transport.  Playwright remains available as a fallback when
``auth_method="playwright"`` is configured.
"""

from __future__ import annotations

import gc
import time
from functools import partial
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from inspire.platform.web.browser_api.core import (
    PLAYWRIGHT_INSTALL_HINT,
    _browser_api_path,
    _get_base_url,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.platform.web.browser_api.rtunnel.http_lab_resolution import (
    get_jupyter_token as _http_get_jupyter_token,
    resolve_lab_url_http,
)
from inspire.platform.web.session import WebSession, get_web_session
from inspire.platform.web.browser_api.rtunnel.logging import trace_event, update_trace_summary

import logging

_log = logging.getLogger("inspire.platform.web.browser_api.playwright_notebooks")

# A notebook reports RUNNING before JupyterLab inside it has finished booting, and
# the first POST /api/terminals is what usually loses that race (worse right after a
# cold image pull). Retry briefly rather than reporting a broken notebook.
_TERMINAL_CREATE_ATTEMPTS = 4
_TERMINAL_CREATE_BACKOFF_S = 3.0


# ---------------------------------------------------------------------------
# Jupyter navigation
# ---------------------------------------------------------------------------


def get_jupyter_token(
    notebook_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 60,
) -> tuple[str, str, str]:
    """Resolve lab URL and extract (base_url, token, host) via HTTP.

    Returns:
        Tuple of (base_url, token, host) where base_url is the Jupyter server
        base URL (without /lab), token is the authentication token, and host
        is the hostname.

    Raises:
        RuntimeError: if the lab URL cannot be resolved.
        SessionExpiredError: if the server returns 401.
    """
    if session is None:
        session = get_web_session()
    return _http_get_jupyter_token(notebook_id, session, timeout=timeout)


def _is_lab_like_url(url: str, *, notebook_lab_pattern: str) -> bool:
    value = str(url or "")
    if not value:
        return False

    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if "notebook-inspire" in parsed.netloc and path.endswith("/lab"):
        return True
    if notebook_lab_pattern.rstrip("/") in parsed.path and path.endswith("/lab"):
        return True
    if "/jupyter/" in parsed.path and path.endswith("/lab"):
        return True
    return False


def _find_lab_handle(page, *, notebook_lab_pattern: str):  # noqa: ANN001
    for fr in page.frames:
        if _is_lab_like_url(fr.url or "", notebook_lab_pattern=notebook_lab_pattern):
            return fr

    page_url = getattr(page, "url", "") or ""
    if _is_lab_like_url(page_url, notebook_lab_pattern=notebook_lab_pattern):
        return page

    return None


def _wait_for_lab_handle(
    page,  # noqa: ANN001
    *,
    notebook_lab_pattern: str,
    timeout_s: float,
):
    start = time.time()
    while time.time() - start < timeout_s:
        handle = _find_lab_handle(page, notebook_lab_pattern=notebook_lab_pattern)
        if handle is not None:
            trace_event(
                "lab_handle_detected",
                handle_type="page" if handle is page else "frame",
                url=getattr(handle, "url", ""),
            )
            return handle
        page.wait_for_timeout(500)
    return None


def open_notebook_lab(
    page, *, notebook_id: str, timeout: int = 60000, session=None
):  # noqa: ANN001
    """Open the notebook's JupyterLab and return the lab frame/page handle."""
    base_url = _get_base_url()
    timeout_ms = max(int(timeout), 10000)
    timeout_s = max(timeout_ms // 1000, 10)
    trace_event("lab_navigation_start", notebook_id=notebook_id, timeout_ms=timeout_ms)

    if session is None:
        session = get_web_session()

    # Try HTTP resolution first (Phase 1: pure HTTP path)
    try:
        http_lab_url = resolve_lab_url_http(notebook_id, session, timeout=max(10, timeout_s))
        if http_lab_url:
            trace_event("lab_navigation_http", notebook_id=notebook_id, lab_url=http_lab_url)
            page.goto(
                http_lab_url,
                timeout=timeout_ms,
                wait_until="domcontentloaded",
            )
            notebook_lab_pattern = _browser_api_path("/notebook/lab/")
            lab_handle = _wait_for_lab_handle(
                page,
                notebook_lab_pattern=notebook_lab_pattern,
                timeout_s=min(10.0, timeout_s / 2.0),
            )
            if lab_handle is not None:
                update_trace_summary(lab_resolution="http_direct")
                return lab_handle
            trace_event("lab_navigation_http_no_handle", notebook_id=notebook_id)
    except Exception as exc:
        trace_event("lab_navigation_http_failed", notebook_id=notebook_id, error=str(exc))

    # Try to get the actual jupyter_url from notebook API first
    jupyter_url = None
    try:
        from inspire.platform.web.browser_api.core import _request_json as browser_request

        data = browser_request(
            session,
            "GET",
            f"/api/v1/notebook/{notebook_id}",
            referer=f"{base_url}/jobs/interactiveModeling",
            timeout=10,
        )
        notebook_data = data.get("data", {})
        jupyter_url = notebook_data.get("jupyter_url") or notebook_data.get("proxy_uri")
        if jupyter_url:
            trace_event("lab_navigation_jupyter_url", jupyter_url=jupyter_url)
    except Exception as e:
        trace_event("lab_navigation_jupyter_url_fetch_failed", error=str(e))

    # If we have jupyter_url, navigate directly to it
    if jupyter_url:
        try:
            page.goto(
                jupyter_url,
                timeout=timeout_ms,
                wait_until="domcontentloaded",
            )
            notebook_lab_pattern = _browser_api_path("/notebook/lab/")
            lab_handle = _wait_for_lab_handle(
                page,
                notebook_lab_pattern=notebook_lab_pattern,
                timeout_s=min(10.0, timeout_s / 2.0),
            )
            if lab_handle is not None:
                update_trace_summary(lab_resolution="jupyter_url_direct")
                return lab_handle
        except Exception as e:
            trace_event("lab_navigation_jupyter_url_failed", error=str(e))

    # Fall back to original navigation methods
    page.goto(
        f"{base_url}/ide?notebook_id={notebook_id}",
        timeout=timeout_ms,
        wait_until="domcontentloaded",
    )

    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    frame_probe_s = min(10.0, max(4.0, timeout_s / 6.0))
    lab_handle = _wait_for_lab_handle(
        page,
        notebook_lab_pattern=notebook_lab_pattern,
        timeout_s=frame_probe_s,
    )
    if lab_handle is not None:
        update_trace_summary(lab_resolution="ide_frame_probe")
        return lab_handle

    notebook_lab_prefix = _browser_api_path("/notebook/lab").rstrip("/")
    direct_lab_url = f"{base_url}{notebook_lab_prefix}/{notebook_id}/"
    trace_event("lab_navigation_fallback", direct_lab_url=direct_lab_url)
    elapsed_ms = int(frame_probe_s * 1000)
    remaining_ms = max(10000, timeout_ms - elapsed_ms)
    direct_timeout_ms = min(remaining_ms, 20000)
    page.goto(
        direct_lab_url,
        timeout=direct_timeout_ms,
        wait_until="domcontentloaded",
    )
    lab_handle = _wait_for_lab_handle(
        page,
        notebook_lab_pattern=notebook_lab_pattern,
        timeout_s=min(5.0, max(1.0, remaining_ms / 1000.0)),
    )
    if lab_handle is not None:
        update_trace_summary(lab_resolution="direct_lab_fallback")
        return lab_handle

    frame_urls = [fr.url for fr in getattr(page, "frames", []) if getattr(fr, "url", "")]
    trace_event(
        "lab_navigation_failed",
        final_page_url=getattr(page, "url", "") or "(empty)",
        frame_count=len(frame_urls),
    )
    update_trace_summary(
        lab_resolution="failed",
        last_error="Failed to resolve notebook JupyterLab to a usable frame or page.",
    )
    raise RuntimeError(
        "Failed to resolve notebook JupyterLab to a usable frame or page.\n"
        f"Final page URL: {getattr(page, 'url', '') or '(empty)'}\n"
        f"Frame URLs: {frame_urls or ['(none)']}"
    )


def build_jupyter_proxy_url(lab_url: str, *, port: int) -> str:
    """Build a Jupyter proxy URL for the given lab URL and port."""
    parsed = urlsplit(lab_url)
    query_token = parse_qs(parsed.query).get("token", [None])[0]

    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    if notebook_lab_pattern.lstrip("/") in lab_url:
        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/lab"):
            base_path = base_path[:-4]
        if not base_path.endswith("/"):
            base_path = base_path + "/"
        base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
        proxy_url = f"{base_url}proxy/{port}/"
        if query_token:
            return f"{proxy_url}?{urlencode({'token': query_token})}"
        return proxy_url

    path_parts = [part for part in parsed.path.split("/") if part]
    path_token = None
    try:
        jupyter_index = path_parts.index("jupyter")
        if len(path_parts) > jupyter_index + 2:
            path_token = path_parts[jupyter_index + 2]
    except ValueError:
        path_token = None

    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/lab"):
        base_path = base_path[:-4]
    proxy_path = f"{base_path}/proxy/{port}/"

    token = query_token or path_token
    query = urlencode({"token": token}) if token else ""
    return urlunsplit((parsed.scheme, parsed.netloc, proxy_path, query, ""))


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def _send_command_via_terminal_ws(
    *,
    context,
    lab_frame,
    command: str,
    timeout_ms: int,
    completion_marker: str | None = None,
) -> bool:
    from inspire.platform.web.browser_api.rtunnel import (
        _build_terminal_websocket_url,
        _create_terminal_via_api,
        _delete_terminal_via_api,
        _send_terminal_command_via_websocket,
    )

    term_name = _create_terminal_via_api(context, lab_frame.url)
    if not term_name:
        return False

    try:
        ws_url = _build_terminal_websocket_url(lab_frame.url, term_name)
        return _send_terminal_command_via_websocket(
            lab_frame,
            ws_url=ws_url,
            command=command,
            timeout_ms=timeout_ms,
            completion_marker=completion_marker,
        )
    finally:
        _delete_terminal_via_api(context, lab_url=lab_frame.url, term_name=term_name)


def _run_command_in_notebook_http(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    timeout: int = 60,
    completion_marker: str | None = None,
    capture_output: bool = True,
) -> dict[str, Any]:
    """HTTP-based sync implementation for run_command_in_notebook."""
    from inspire.platform.web.browser_api.rtunnel.transports import PythonWsTransport

    if session is None:
        session = get_web_session()

    _log.info("Running command in notebook terminal via HTTP...")

    try:
        base_url, token, _host = get_jupyter_token(notebook_id, session, timeout=timeout)
        transport = PythonWsTransport(session, base_url, token=token)

        term_name = None
        for attempt in range(1, _TERMINAL_CREATE_ATTEMPTS + 1):
            term_name = transport.create_terminal()
            if term_name:
                break
            if attempt < _TERMINAL_CREATE_ATTEMPTS:
                _log.info(
                    "Jupyter terminal not available yet (attempt %d/%d), retrying",
                    attempt,
                    _TERMINAL_CREATE_ATTEMPTS,
                )
                time.sleep(_TERMINAL_CREATE_BACKOFF_S)

        if not term_name:
            transport.cleanup()
            return {
                "success": False,
                "stdout": "",
                "error": (
                    f"Could not open a Jupyter terminal after {_TERMINAL_CREATE_ATTEMPTS} "
                    "attempts. The notebook reports RUNNING but its JupyterLab is most "
                    "likely still starting (common right after startup or a cold image "
                    "pull) - retry the command, and if it keeps failing open the "
                    "notebook in a browser to check JupyterLab."
                ),
            }

        try:
            result = transport.send_command(command, completion_marker or "", timeout)
            stdout = result.get("stdout", "")
            diagnostics = {k: v for k, v in result.items() if k != "stdout"}

            if completion_marker and not (result.get("markerFound") or result.get("ok")):
                return {
                    "success": False,
                    "stdout": stdout,
                    "error": "Completion marker not observed before timeout",
                    "diagnostics": diagnostics,
                }

            if capture_output:
                return {
                    "success": True,
                    "stdout": stdout,
                    "error": None,
                    "diagnostics": diagnostics,
                }
            return {"success": True, "stdout": "", "error": None, "diagnostics": diagnostics}
        finally:
            transport.cleanup()

    except Exception as e:
        error_str = str(e)
        hint = (
            "Use 'inspire bridge exec -b <bridge> -- <command>' instead "
            "(SSH tunnel, no browser required)."
        )
        return {"success": False, "stdout": "", "error": error_str, "hint": hint}


def _run_command_in_notebook_playwright(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
    completion_marker: str | None = None,
    capture_output: bool = True,
) -> dict[str, Any]:
    """Playwright-based sync implementation for run_command_in_notebook (fallback)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            f"{PLAYWRIGHT_INSTALL_HINT}\nOr switch to HTTP auth: export INSPIRE_AUTH_METHOD=http"
        ) from None

    from inspire.platform.web.browser_api.rtunnel import (
        _focus_terminal_input,
        _open_or_create_terminal,
    )
    from inspire.platform.web.browser_api.rtunnel.terminal import (
        _run_terminal_command_capture_via_websocket,
    )

    if session is None:
        session = get_web_session()

    _log.info("Running command in notebook terminal via Playwright...")

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()

        try:
            try:
                lab_frame = open_notebook_lab(page, notebook_id=notebook_id, session=session)
                timeout_ms = max(int(timeout * 1000), 1000)

                try:
                    lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=30000)
                except Exception:
                    pass

                if capture_output:
                    # Use capture mode to get stdout
                    result = _run_terminal_command_capture_via_websocket(
                        context=context,
                        lab_frame=lab_frame,
                        batch_cmd=command,
                        timeout_ms=timeout_ms,
                        completion_marker=completion_marker or "",
                    )
                    if result.get("ok"):
                        gc.collect()
                        return {
                            "success": True,
                            "stdout": result.get("output", ""),
                            "error": None,
                            "diagnostics": result.get("diagnostics", {}),
                        }
                    if completion_marker:
                        gc.collect()
                        return {
                            "success": False,
                            "stdout": result.get("output", ""),
                            "error": "Completion marker not observed before timeout",
                            "diagnostics": result.get("diagnostics", {}),
                        }
                    # WebSocket capture failed — fall through to keyboard fallback
                    # so we still execute the command even if we can't capture output

                # Try WebSocket exec (no capture) or fall back to keyboard
                if not _send_command_via_terminal_ws(
                    context=context,
                    lab_frame=lab_frame,
                    command=command,
                    timeout_ms=timeout_ms,
                    completion_marker=completion_marker,
                ):
                    if completion_marker:
                        gc.collect()
                        return {
                            "success": False,
                            "stdout": "",
                            "error": "Completion marker not observed before timeout",
                        }
                    terminal_opened, _term_name = _open_or_create_terminal(context, page, lab_frame)
                    if not terminal_opened:
                        gc.collect()
                        return {
                            "success": False,
                            "stdout": "",
                            "error": "Failed to open Jupyter terminal",
                        }

                    if not _focus_terminal_input(lab_frame, page):
                        gc.collect()
                        return {
                            "success": False,
                            "stdout": "",
                            "error": "Failed to focus Jupyter terminal input",
                        }

                    page.keyboard.insert_text(command)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(min(timeout_ms, 1000))

                gc.collect()
                return {"success": True, "stdout": "", "error": None}

            except RuntimeError as e:
                # JupyterLab not accessible — fall back to keyboard automation on the raw page
                if "Failed to resolve notebook JupyterLab" not in str(e):
                    raise
                terminal_opened, _term_name = _open_or_create_terminal(context, page, page)
                gc.collect()
                if not terminal_opened:
                    return {
                        "success": False,
                        "stdout": "",
                        "error": "Failed to open Jupyter terminal",
                    }
                if not _focus_terminal_input(page, page):
                    return {
                        "success": False,
                        "stdout": "",
                        "error": "Failed to focus Jupyter terminal input",
                    }
                page.keyboard.insert_text(command)
                page.keyboard.press("Enter")
                timeout_ms = max(int(timeout * 1000), 1000)
                page.wait_for_timeout(min(timeout_ms, 1000))
                if completion_marker:
                    return {
                        "success": False,
                        "stdout": "",
                        "error": "Completion marker not observed before timeout",
                    }
                return {"success": True, "stdout": "", "error": None}

            except Exception as e:
                gc.collect()
                error_str = str(e)
                hint = (
                    "Use 'inspire bridge exec -b <bridge> -- <command>' instead "
                    "(SSH tunnel, no browser required)."
                )
                if any(
                    keyword in error_str.lower()
                    for keyword in ("display", "cannot open display", "xvfb", "no sandbox")
                ):
                    hint = (
                        "No display server available. "
                        "Use 'inspire bridge exec -b <bridge> -- <command>' instead "
                        "(SSH tunnel, no browser required)."
                    )
                elif "inotify" in error_str.lower() or "too many open files" in error_str.lower():
                    hint = (
                        "System file descriptor limit reached. "
                        "Use 'inspire bridge exec -b <bridge> -- <command>' instead "
                        "(SSH tunnel, no browser required), or restart this environment."
                    )
                elif (
                    "target page" in error_str.lower()
                    or "context" in error_str.lower()
                    or "browser" in error_str.lower()
                ):
                    hint = (
                        "Browser crashed or was closed unexpectedly. "
                        "Use 'inspire bridge exec -b <bridge> -- <command>' instead "
                        "(SSH tunnel, no browser required)."
                    )
                return {"success": False, "stdout": "", "error": error_str, "hint": hint}
        finally:
            try:
                context.close()
            finally:
                browser.close()


def run_command_in_notebook(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,  # ignored for HTTP method
    timeout: int = 60,
    completion_marker: str | None = None,
    capture_output: bool = True,
) -> dict[str, Any]:
    """Run a command in a notebook's Jupyter terminal.

    Uses the HTTP WebSocket transport by default (auth_method="http").
    Falls back to Playwright browser automation when auth_method="playwright".

    Args:
        capture_output: If True, capture and return stdout. If False, only return success status.

    Returns:
        Dict with keys: success (bool), stdout (str), error (str|None)
    """
    from inspire.platform.web.session.auth import _get_auth_method

    auth_method = _get_auth_method()

    if auth_method == "http":
        runner = partial(
            _run_command_in_notebook_http,
            notebook_id=notebook_id,
            command=command,
            session=session,
            timeout=timeout,
            completion_marker=completion_marker,
            capture_output=capture_output,
        )
    else:
        runner = partial(
            _run_command_in_notebook_playwright,
            notebook_id=notebook_id,
            command=command,
            session=session,
            headless=headless,
            timeout=timeout,
            completion_marker=completion_marker,
            capture_output=capture_output,
        )

    if _in_asyncio_loop():
        return _run_in_thread(runner)
    return runner()


__all__ = [
    "build_jupyter_proxy_url",
    "get_jupyter_token",
    "open_notebook_lab",
    "run_command_in_notebook",
]
