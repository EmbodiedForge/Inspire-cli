"""JupyterLab terminal: REST API creation, DOM fallbacks, WebSocket command dispatch."""

from __future__ import annotations

import time
from typing import Any

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        pass


from .commands import SETUP_DONE_MARKER, SSHD_MISSING_MARKER, SSH_SERVER_MISSING_MARKER
from ._jupyter import (
    _build_jupyter_xsrf_headers,
    _extract_jupyter_token,
    _jupyter_server_base,
)
from .logging import trace_event, update_trace_summary

import logging

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel")


def _create_terminal_via_api(context: Any, lab_url: str) -> str | None:
    """Create a JupyterLab terminal via REST API.

    Uses ``context.request`` which shares the browser session's cookies.
    JupyterLab requires an ``_xsrf`` cookie value in the ``X-XSRFToken``
    header for state-changing requests.
    Returns the terminal name (e.g. ``"1"``) on success, or ``None``.
    """
    base = _jupyter_server_base(lab_url)
    api_url = f"{base}api/terminals"
    try:
        headers = _build_jupyter_xsrf_headers(context)
        resp = context.request.post(api_url, headers=headers, timeout=10000)
        trace_event("terminal_api_create_response", status=resp.status)
        if resp.status in (200, 201):
            data = resp.json()
            trace_event("terminal_api_create_success", term_name=data.get("name"))
            return data.get("name")
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
    ) as exc:
        trace_event("terminal_api_create_failed", error=exc)
        pass
    return None


def _delete_terminal_via_api(
    context: Any,
    *,
    lab_url: str,
    term_name: str,
) -> bool:
    """Delete a Jupyter terminal by name (best-effort cleanup)."""
    from urllib.parse import quote

    safe_term_name = (term_name or "").strip()
    if not safe_term_name:
        return False

    base = _jupyter_server_base(lab_url)
    api_url = f"{base}api/terminals/{quote(safe_term_name, safe='')}"
    try:
        headers = _build_jupyter_xsrf_headers(context)
        resp = context.request.delete(api_url, headers=headers, timeout=5000)
        trace_event("terminal_api_delete_response", term_name=safe_term_name, status=resp.status)
        return resp.status in (200, 204, 404)
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
    ) as exc:
        trace_event("terminal_api_delete_failed", term_name=safe_term_name, error=exc)
        return False


def _build_terminal_websocket_url(lab_url: str, term_name: str) -> str:
    from urllib.parse import urlencode, urlsplit, urlunsplit

    base = _jupyter_server_base(lab_url)
    parsed = urlsplit(base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    ws_path = f"{base_path}terminals/websocket/{term_name}"

    token = _extract_jupyter_token(lab_url)
    query = urlencode({"token": token}) if token else ""
    ws_url = urlunsplit((scheme, parsed.netloc, ws_path, query, ""))
    trace_event("terminal_ws_url_built", term_name=term_name, ws_url=ws_url)
    return ws_url


def _send_terminal_command_via_websocket(
    page_or_frame: Any,
    *,
    ws_url: str,
    command: str,
    timeout_ms: int = 5000,
    completion_marker: str | None = None,
    error_markers: list[str] | None = None,
    detected_errors: list[str] | None = None,
    diagnostics_out: dict[str, Any] | None = None,
) -> bool:
    """Send a command to a Jupyter terminal via WebSocket.

    *page_or_frame* should be the Playwright frame whose origin matches the
    WebSocket URL (typically the JupyterLab iframe, not the outer page) so
    that the browser creates a same-origin WebSocket connection.

    Waits for a shell prompt (``["stdout", ...]`` message) before sending
    stdin so that the command is not lost if bash hasn't initialized yet.

    When *completion_marker* is set, the function keeps the WebSocket open
    after sending and waits until the marker string appears in a subsequent
    stdout message.  This allows callers to block until a setup script
    finishes (e.g. ``INSPIRE_RTUNNEL_SETUP_DONE``).

    When *error_markers* is provided, stdout received after sending is
    accumulated in a rolling buffer and checked for each marker.  Any
    matches are appended to *detected_errors* (if supplied).  The return
    type stays ``bool`` for backward compatibility.
    """
    stdin_payload = command.rstrip("\r\n") + "\r"
    try:
        result = page_or_frame.evaluate(
            """
                async ({ wsUrl, stdinData, timeoutMs, promptTimeoutMs, marker, errorMarkers, chunkSize, chunkDelayMs }) => {
                  return await new Promise((resolve) => {
                    let settled = false;
                    let sent = false;
                    let socket = null;
                    const foundErrors = [];
                    let afterSendBuf = "";
                    let wsConnected = false;
                    let promptDetected = false;
                    let stdoutReceived = false;
                    let wsCloseCode = null;
                    let wsCloseReason = "";
                    const startTime = Date.now();
                    const finish = (ok) => {
                      if (settled) return;
                      settled = true;
                      try {
                        if (socket) socket.close();
                      } catch (_) {}
                      resolve({
                        ok, errors: foundErrors,
                        diagnostics: {
                          wsConnected, promptDetected, commandSent: sent,
                          stdoutReceived, stdoutLen: afterSendBuf.length,
                          wsCloseCode, wsCloseReason,
                          elapsed: Date.now() - startTime
                        }
                      });
                    };

                    const timer = setTimeout(() => finish(false), timeoutMs);

                    const checkErrors = (text) => {
                      afterSendBuf += text;
                      for (const em of errorMarkers) {
                        if (em && afterSendBuf.includes(em) && !foundErrors.includes(em)) {
                          foundErrors.push(em);
                        }
                      }
                    };

                    const CHUNK = chunkSize;
                    const DELAY = chunkDelayMs;
                    const doSend = () => {
                      if (sent || settled) return;
                      sent = true;
                      const chunks = [];
                      for (let i = 0; i < stdinData.length; i += CHUNK)
                        chunks.push(stdinData.slice(i, i + CHUNK));
                      let idx = 0;
                      const next = () => {
                        if (settled) return;
                        try {
                          socket.send(JSON.stringify(["stdin", chunks[idx]]));
                        } catch (_) {
                          clearTimeout(timer);
                          finish(false);
                          return;
                        }
                        idx++;
                        if (idx < chunks.length) {
                          setTimeout(next, DELAY);
                        } else if (!marker) {
                          setTimeout(() => {
                            clearTimeout(timer);
                            finish(true);
                          }, 180);
                        }
                      };
                      next();
                    };

                    try {
                      socket = new WebSocket(wsUrl);
                    } catch (_) {
                      clearTimeout(timer);
                      finish(false);
                      return;
                    }

                    let stdoutBuf = "";
                    const promptRe = /[$#]\\s*$/;
                    socket.addEventListener("message", (ev) => {
                      try {
                        const msg = JSON.parse(ev.data);
                        if (Array.isArray(msg) && msg[0] === "stdout") {
                          const text = String(msg[1]);
                          stdoutReceived = true;
                          if (!sent) {
                            stdoutBuf += text;
                            if (promptRe.test(stdoutBuf)) {
                              promptDetected = true;
                              doSend();
                            }
                          } else {
                            if (errorMarkers.length > 0) {
                              checkErrors(text);
                            }
                            if (marker && text.includes(marker)) {
                              clearTimeout(timer);
                              finish(true);
                            }
                          }
                        }
                      } catch (_) {}
                    });

                    socket.addEventListener("open", () => {
                      wsConnected = true;
                      // Fall back after promptTimeoutMs in case
                      // the shell never emits a recognisable prompt.
                      setTimeout(() => doSend(), promptTimeoutMs);
                    });

                    socket.addEventListener("error", () => {
                      clearTimeout(timer);
                      finish(false);
                    });

                    socket.addEventListener("close", (ev) => {
                      wsCloseCode = ev.code;
                      wsCloseReason = ev.reason || "";
                      if (!settled) {
                        clearTimeout(timer);
                        finish(false);
                      }
                    });
                  });
                }
                """,
            {
                "wsUrl": ws_url,
                "stdinData": stdin_payload,
                "timeoutMs": int(timeout_ms),
                "promptTimeoutMs": min(int(timeout_ms) - 500, 3000),
                "marker": completion_marker or "",
                "errorMarkers": error_markers or [],
                "chunkSize": int(_TERMINAL_STDIN_CHUNK),
                "chunkDelayMs": int(_TERMINAL_STDIN_DELAY_MS),
            },
        )
        if isinstance(result, dict):
            if detected_errors is not None:
                detected_errors.extend(result.get("errors", []))
            if diagnostics_out is not None:
                diagnostics_out.clear()
                diagnostics_out.update(result.get("diagnostics") or {})
                diagnostics_out["ok"] = bool(result.get("ok", False))
                diagnostics_out["errors"] = list(result.get("errors", []))
            ok = bool(result.get("ok", False))
            trace_event(
                "terminal_ws_command_result",
                ok=ok,
                marker=completion_marker or "",
                errors=",".join(result.get("errors", [])),
            )
            if not ok:
                diag = result.get("diagnostics")
                if diag:
                    _log_ws_diagnostics(diag)
            else:
                update_trace_summary(terminal_transport="terminal_ws")
            return ok
        return bool(result)
    except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError) as exc:
        trace_event("terminal_ws_command_exception", error=exc)
        return False


def _run_terminal_command_capture_via_websocket(
    *,
    context: Any,
    lab_frame: Any,
    batch_cmd: str,
    timeout_ms: int,
    completion_marker: str,
) -> dict[str, Any]:
    """Run a terminal command and capture bounded stdout until completion marker."""
    term_name = _create_terminal_via_api(context, lab_frame.url)
    if not term_name:
        return {"ok": False, "output": ""}

    stdin_payload = batch_cmd.rstrip("\r\n") + "\r"
    try:
        ws_url = _build_terminal_websocket_url(lab_frame.url, term_name)
        result = lab_frame.evaluate(
            """
                async ({ wsUrl, stdinData, timeoutMs, promptTimeoutMs, marker, outputCap, chunkSize, chunkDelayMs }) => {
                  return await new Promise((resolve) => {
                    let settled = false;
                    let sent = false;
                    let socket = null;
                    let afterSendBuf = "";
                    const startTime = Date.now();
                    const finish = (ok) => {
                      if (settled) return;
                      settled = true;
                      try {
                        if (socket) socket.close();
                      } catch (_) {}
                      resolve({
                        ok,
                        output: afterSendBuf,
                        diagnostics: { elapsed: Date.now() - startTime }
                      });
                    };
                    const timer = setTimeout(() => finish(false), timeoutMs);
                    const CHUNK = chunkSize;
                    const DELAY = chunkDelayMs;
                    const doSend = () => {
                      if (sent || settled) return;
                      sent = true;
                      const chunks = [];
                      for (let i = 0; i < stdinData.length; i += CHUNK)
                        chunks.push(stdinData.slice(i, i + CHUNK));
                      let idx = 0;
                      const next = () => {
                        if (settled) return;
                        try {
                          socket.send(JSON.stringify(["stdin", chunks[idx]]));
                        } catch (_) {
                          clearTimeout(timer);
                          finish(false);
                          return;
                        }
                        idx++;
                        if (idx < chunks.length) {
                          setTimeout(next, DELAY);
                        }
                      };
                      next();
                    };
                    try {
                      socket = new WebSocket(wsUrl);
                    } catch (_) {
                      clearTimeout(timer);
                      finish(false);
                      return;
                    }
                    let stdoutBuf = "";
                    const promptRe = /[$#]\\s*$/;
                    socket.addEventListener("message", (ev) => {
                      try {
                        const msg = JSON.parse(ev.data);
                        if (Array.isArray(msg) && msg[0] === "stdout") {
                          const text = String(msg[1]);
                          if (!sent) {
                            stdoutBuf += text;
                            if (promptRe.test(stdoutBuf)) {
                              doSend();
                            }
                          } else {
                            afterSendBuf += text;
                            if (afterSendBuf.length > outputCap) {
                              afterSendBuf = afterSendBuf.slice(-outputCap);
                            }
                            if (marker && afterSendBuf.includes(marker)) {
                              clearTimeout(timer);
                              finish(true);
                            }
                          }
                        }
                      } catch (_) {}
                    });
                    socket.addEventListener("open", () => {
                      setTimeout(() => doSend(), promptTimeoutMs);
                    });
                    socket.addEventListener("error", () => {
                      clearTimeout(timer);
                      finish(false);
                    });
                    socket.addEventListener("close", () => {
                      if (!settled) {
                        clearTimeout(timer);
                        finish(false);
                      }
                    });
                  });
                }
            """,
            {
                "wsUrl": ws_url,
                "stdinData": stdin_payload,
                "timeoutMs": int(timeout_ms),
                "promptTimeoutMs": min(int(timeout_ms) - 500, 3000),
                "marker": completion_marker,
                "outputCap": 20000,
                "chunkSize": int(_TERMINAL_STDIN_CHUNK),
                "chunkDelayMs": int(_TERMINAL_STDIN_DELAY_MS),
            },
        )
        trace_event("terminal_ws_capture_command_result", ok=bool(result.get("ok")))
        return result if isinstance(result, dict) else {"ok": False, "output": ""}
    except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError) as exc:
        trace_event("terminal_ws_capture_command_exception", error=exc)
        return {"ok": False, "output": ""}
    finally:
        _delete_terminal_via_api(context, lab_url=lab_frame.url, term_name=term_name)


def _send_setup_command_via_terminal_ws(
    *,
    context: Any,
    lab_frame: Any,
    batch_cmd: str,
    detected_errors: list[str] | None = None,
    diagnostics_out: dict[str, Any] | None = None,
) -> bool:
    term_name = _create_terminal_via_api(context, lab_frame.url)
    if not term_name:
        return False

    try:
        ws_url = _build_terminal_websocket_url(lab_frame.url, term_name)
        return _send_terminal_command_via_websocket(
            lab_frame,
            ws_url=ws_url,
            command=batch_cmd,
            timeout_ms=120000,
            completion_marker=SETUP_DONE_MARKER,
            error_markers=[SSHD_MISSING_MARKER, SSH_SERVER_MISSING_MARKER],
            detected_errors=detected_errors,
            diagnostics_out=diagnostics_out,
        )
    finally:
        _delete_terminal_via_api(context, lab_url=lab_frame.url, term_name=term_name)


def _build_batch_setup_script(cmd_lines: list[str]) -> str:
    """Encode setup commands as a heredoc-wrapped base64 bash payload."""
    import base64

    script = "\n".join(cmd_lines) + "\n"
    encoded = base64.b64encode(script.encode()).decode()
    wrapped = "\n".join(encoded[idx : idx + 160] for idx in range(0, len(encoded), 160))
    marker = "__INSPIRE_RTUNNEL_B64__"
    return f"cat <<'{marker}' | base64 -d | bash\n{wrapped}\n{marker}"


_TERMINAL_TAB_SELECTOR = "li.lm-TabBar-tab:has-text('Terminal'), li.lm-TabBar-tab:has-text('终端')"
_TERMINAL_CARD_SELECTOR = (
    "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
)
_TERMINAL_INPUT_SELECTORS = (
    "textarea.xterm-helper-textarea",
    "div.xterm-helper-textarea textarea",
)
_FAST_API_XTERM_ATTACH_TIMEOUT_MS = 1600
_FAST_API_MENU_READY_TIMEOUT_MS = 2500
_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS = 900
_FAST_TERMINAL_CARD_WAIT_TIMEOUT_MS = 3500
_FAST_TERMINAL_CARD_CLICK_TIMEOUT_MS = 2500
_FAST_MENU_ACTION_TIMEOUT_MS = 1800
_API_TERMINAL_PROGRESSIVE_WAIT_MS = 1800
_API_TERMINAL_RECOVERY_WAIT_MS = 900
_API_TERMINAL_POLL_MS = 220
_API_TERMINAL_TAB_POKE_INTERVAL_MS = 1200
_FOCUS_INPUT_WAIT_TIMEOUT_MS = 900
_FOCUS_INPUT_CLICK_TIMEOUT_MS = 500
_FOCUS_TAB_CLICK_TIMEOUT_MS = 450
_FOCUS_TEXTAREA_ATTACH_TIMEOUT_MS = 3000
_FOCUS_RETRY_PASSES = 4
_TERMINAL_STDIN_CHUNK = 512
_TERMINAL_STDIN_DELAY_MS = 20


def _wait_for_terminal_surface(
    lab_frame: Any,
    *,
    timeout_ms: int,
) -> bool:
    try:
        lab_frame.locator(".xterm").first.wait_for(state="attached", timeout=timeout_ms)
        return True
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
        pass

    for selector in _TERMINAL_INPUT_SELECTORS:
        try:
            if lab_frame.locator(selector).first.count() > 0:
                return True
        except (PlaywrightError, RuntimeError, AttributeError, TypeError, ValueError):
            pass
    return False


def _wait_for_terminal_surface_progressive(
    lab_frame: Any,
    page: Any,
    *,
    total_timeout_ms: int,
    poll_ms: int = _API_TERMINAL_POLL_MS,
    tab_poke_interval_ms: int = _API_TERMINAL_TAB_POKE_INTERVAL_MS,
) -> bool:
    start = time.monotonic()
    last_tab_poke = -tab_poke_interval_ms
    min_probe_ms = 80

    while True:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        remaining_ms = total_timeout_ms - elapsed_ms
        if remaining_ms <= 0:
            return False

        probe_timeout_ms = max(min_probe_ms, min(280, remaining_ms))
        if _wait_for_terminal_surface(lab_frame, timeout_ms=probe_timeout_ms):
            return True

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if elapsed_ms - last_tab_poke >= tab_poke_interval_ms:
            _click_terminal_tab(
                lab_frame,
                page,
                timeout_ms=min(_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS, 350),
                settle_ms=40,
            )
            last_tab_poke = elapsed_ms

        elapsed_ms = int((time.monotonic() - start) * 1000)
        remaining_ms = total_timeout_ms - elapsed_ms
        if remaining_ms <= 0:
            return False
        page.wait_for_timeout(max(40, min(poll_ms, remaining_ms)))


def _wait_for_file_menu_ready(
    lab_frame: Any,
    *,
    timeout_ms: int,
) -> bool:
    per_label_timeout = max(300, timeout_ms // 2)
    for label in ("File", "文件"):
        try:
            lab_frame.get_by_role("menuitem", name=label).first.wait_for(
                state="visible",
                timeout=per_label_timeout,
            )
            return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass
    return False


def _click_terminal_tab(
    lab_frame: Any,
    page: Any,
    *,
    timeout_ms: int,
    settle_ms: int = 80,
) -> bool:
    try:
        term_tab = lab_frame.locator(_TERMINAL_TAB_SELECTOR).first
        if term_tab.count() <= 0:
            return False
        term_tab.click(timeout=timeout_ms)
        if settle_ms > 0:
            page.wait_for_timeout(settle_ms)
        return True
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError, TypeError):
        return False


def _open_terminal_from_file_menu(
    lab_frame: Any,
    *,
    action_timeout_ms: int,
) -> bool:
    for labels in (("File", "New", "Terminal"), ("文件", "新建", "终端")):
        file_label, new_label, terminal_label = labels
        try:
            lab_frame.get_by_role("menuitem", name=file_label).first.click(
                timeout=action_timeout_ms
            )
            lab_frame.get_by_role("menuitem", name=new_label).first.hover(timeout=action_timeout_ms)
            lab_frame.get_by_role("menuitem", name=terminal_label).first.click(
                timeout=action_timeout_ms
            )
            return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass
    return False


def _verify_terminal_focus(lab_frame: Any) -> bool:
    """Check that document.activeElement is the xterm textarea."""
    try:
        tag = lab_frame.evaluate("document.activeElement?.tagName?.toLowerCase()")
        cls = lab_frame.evaluate("document.activeElement?.className || ''")
        return tag == "textarea" and "xterm" in cls
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError, TypeError):
        return False


def _focus_terminal_input(
    lab_frame: Any,
    page: Any,
) -> bool:
    textarea_found = False
    for sel in _TERMINAL_INPUT_SELECTORS:
        try:
            lab_frame.locator(sel).first.wait_for(
                state="attached", timeout=_FOCUS_TEXTAREA_ATTACH_TIMEOUT_MS
            )
            textarea_found = True
            break
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass

    if not textarea_found:
        return False

    for pass_idx in range(_FOCUS_RETRY_PASSES):
        if pass_idx == 0:
            _dismiss_terminal_dialog_once(lab_frame=lab_frame, page=page, settle_ms=80)

        try:
            xterm_el = lab_frame.locator(".xterm").first
            if xterm_el.count() > 0:
                xterm_el.click(timeout=_FOCUS_INPUT_CLICK_TIMEOUT_MS, force=True)
                page.wait_for_timeout(40)
                if _verify_terminal_focus(lab_frame):
                    return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass

        try:
            ok = lab_frame.evaluate(
                """(() => {
                    const xterm = document.querySelector('.xterm');
                    if (xterm) {
                        xterm.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        xterm.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    }
                    const el = document.querySelector('textarea.xterm-helper-textarea');
                    if (!el) return false;
                    el.focus();
                    return document.activeElement === el;
                })()"""
            )
            if ok:
                return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError, TypeError):
            pass

        _click_terminal_tab(
            lab_frame,
            page,
            timeout_ms=_FOCUS_TAB_CLICK_TIMEOUT_MS,
            settle_ms=40,
        )
        page.wait_for_timeout(120)

    return False


def _log_ws_diagnostics(diag: dict) -> None:
    """Write structured WS diagnostics to stderr (only on failure)."""
    parts = [
        f"wsConnected={diag.get('wsConnected')}",
        f"promptDetected={diag.get('promptDetected')}",
        f"commandSent={diag.get('commandSent')}",
        f"stdoutReceived={diag.get('stdoutReceived')}",
        f"stdoutLen={diag.get('stdoutLen', 0)}",
        f"wsCloseCode={diag.get('wsCloseCode')}",
        f"wsCloseReason={diag.get('wsCloseReason', '')!r}",
        f"elapsed={diag.get('elapsed', 0)}ms",
    ]
    trace_event("terminal_ws_diagnostics", **diag)
    _log_terminal_status("  [ws-diagnostics] " + " | ".join(parts))


def _log_terminal_status(message: str) -> None:
    import sys as _sys

    _sys.stderr.write(message + "\n")
    _sys.stderr.flush()


def _wait_for_api_terminal_surface(
    lab_frame: Any,
    page: Any,
) -> bool:
    if _wait_for_terminal_surface(lab_frame, timeout_ms=500):
        return True
    if _wait_for_terminal_surface_progressive(
        lab_frame,
        page,
        total_timeout_ms=_API_TERMINAL_PROGRESSIVE_WAIT_MS,
    ):
        return True
    return _wait_for_terminal_surface_progressive(
        lab_frame,
        page,
        total_timeout_ms=_API_TERMINAL_RECOVERY_WAIT_MS,
    )


def _open_terminal_via_rest_api(
    *,
    context: Any,
    page: Any,
    lab_frame: Any,
) -> tuple[bool, bool, str | None]:
    lab_url = lab_frame.url
    term_name = _create_terminal_via_api(context, lab_url)
    if not term_name:
        trace_event("terminal_rest_path_unavailable")
        return False, False, None

    _log_terminal_status(f"  Created terminal '{term_name}' via REST API.")
    update_trace_summary(terminal_transport="rest_api_terminal")
    server_base = _jupyter_server_base(lab_url)
    term_url = f"{server_base}lab/terminals/{term_name}?reset"
    try:
        lab_frame.goto(term_url, timeout=15000, wait_until="domcontentloaded")
        if _wait_for_terminal_surface(lab_frame, timeout_ms=_FAST_API_XTERM_ATTACH_TIMEOUT_MS):
            trace_event("terminal_rest_surface_ready", term_name=term_name)
            return True, True, term_name
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError) as _nav_err:
        trace_event("terminal_rest_navigation_failed", term_name=term_name, error=_nav_err)
        _log_terminal_status(
            f"  REST API terminal created but navigation failed ({type(_nav_err).__name__}: {str(_nav_err)[:150]}), trying DOM fallbacks..."
        )
        return False, True, term_name

    trace_event("terminal_rest_surface_delayed", term_name=term_name)
    _log_terminal_status(
        "  REST API terminal created but xterm not yet visible; continuing with API terminal path."
    )
    return _wait_for_api_terminal_surface(lab_frame, page), True, term_name


def _recover_api_terminal_surface(
    *,
    lab_frame: Any,
    page: Any,
) -> bool:
    if _click_terminal_tab(
        lab_frame,
        page,
        timeout_ms=min(_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS, 500),
        settle_ms=60,
    ) and _wait_for_terminal_surface_progressive(
        lab_frame,
        page,
        total_timeout_ms=900,
    ):
        return True

    menu_ready = _wait_for_file_menu_ready(lab_frame, timeout_ms=_FAST_API_MENU_READY_TIMEOUT_MS)
    if (
        menu_ready
        and _open_terminal_from_file_menu(
            lab_frame,
            action_timeout_ms=_FAST_MENU_ACTION_TIMEOUT_MS,
        )
        and _wait_for_terminal_surface_progressive(
            lab_frame,
            page,
            total_timeout_ms=2200,
        )
    ):
        return True

    return False


def _wait_for_terminal_entry_point(
    *,
    lab_frame: Any,
    api_term_created: bool,
) -> None:
    if api_term_created:
        _wait_for_file_menu_ready(lab_frame, timeout_ms=_FAST_API_MENU_READY_TIMEOUT_MS)
        return

    try:
        lab_frame.locator(_TERMINAL_CARD_SELECTOR).first.wait_for(state="visible", timeout=45000)
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
        _wait_for_file_menu_ready(lab_frame, timeout_ms=45000)


def _dismiss_terminal_dialog_once(
    *,
    lab_frame: Any,
    page: Any,
    settle_ms: int,
) -> bool:
    for label in ("Dismiss", "OK", "Accept", "No", "否", "不接收", "取消", "确定"):
        try:
            btn = lab_frame.get_by_role("button", name=label)
            if btn.count() > 0:
                btn.first.click(timeout=1000)
                page.wait_for_timeout(settle_ms)
                return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass

    for selector in (
        "button.jp-Dialog-button.jp-mod-accept",
        "button.jp-Dialog-close",
        "button[aria-label='Close']",
    ):
        try:
            btn = lab_frame.locator(selector)
            if btn.count() > 0:
                btn.first.click(timeout=1000)
                page.wait_for_timeout(settle_ms)
                return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass

    return False


def _open_terminal_card(
    *,
    lab_frame: Any,
    api_term_created: bool,
) -> bool:
    terminal_card = lab_frame.locator(_TERMINAL_CARD_SELECTOR)
    card_wait_timeout = _FAST_TERMINAL_CARD_WAIT_TIMEOUT_MS if api_term_created else 8000
    card_click_timeout = _FAST_TERMINAL_CARD_CLICK_TIMEOUT_MS if api_term_created else 8000
    try:
        terminal_card.first.wait_for(state="visible", timeout=card_wait_timeout)
        terminal_card.first.click(timeout=card_click_timeout)
        return True
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
        return False


def _open_terminal_card_from_launcher(
    *,
    lab_frame: Any,
    page: Any,
    api_term_created: bool,
) -> bool:
    try:
        launcher_btn = lab_frame.locator(
            "button[title*='Launcher'], button[aria-label*='Launcher']"
        ).first
        if launcher_btn.count() > 0:
            launcher_btn.click(timeout=1200)
            page.wait_for_timeout(150)
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
        return False

    return _open_terminal_card(lab_frame=lab_frame, api_term_created=api_term_created)


def _open_terminal_via_dom_fallback(
    *,
    lab_frame: Any,
    page: Any,
    api_term_created: bool,
) -> bool:
    if _click_terminal_tab(
        lab_frame,
        page,
        timeout_ms=_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS,
        settle_ms=100,
    ):
        return True

    if _open_terminal_card(lab_frame=lab_frame, api_term_created=api_term_created):
        return True
    if _open_terminal_card_from_launcher(
        lab_frame=lab_frame,
        page=page,
        api_term_created=api_term_created,
    ):
        return True

    menu_action_timeout = _FAST_MENU_ACTION_TIMEOUT_MS if api_term_created else 2000
    if _open_terminal_from_file_menu(lab_frame, action_timeout_ms=menu_action_timeout):
        return True

    return api_term_created and _wait_for_terminal_surface(lab_frame, timeout_ms=1200)


def _open_or_create_terminal(
    context: Any,
    page: Any,
    lab_frame: Any,
) -> tuple[bool, str | None]:
    """Open a terminal in JupyterLab.  REST API first, then DOM fallbacks."""
    terminal_ready, api_term_created, term_name = _open_terminal_via_rest_api(
        context=context,
        page=page,
        lab_frame=lab_frame,
    )
    if terminal_ready:
        trace_event("terminal_open_ready", source="rest_api", term_name=term_name)
        return True, term_name

    if api_term_created and _recover_api_terminal_surface(lab_frame=lab_frame, page=page):
        update_trace_summary(terminal_transport="rest_api_recovery")
        trace_event("terminal_open_ready", source="rest_api_recovery", term_name=term_name)
        return True, term_name

    _wait_for_terminal_entry_point(lab_frame=lab_frame, api_term_created=api_term_created)
    _dismiss_terminal_dialog_once(lab_frame=lab_frame, page=page, settle_ms=150)

    if not _open_terminal_via_dom_fallback(
        lab_frame=lab_frame,
        page=page,
        api_term_created=api_term_created,
    ):
        trace_event("terminal_dom_fallback_failed", api_term_created=api_term_created)
        return False, None

    _click_terminal_tab(
        lab_frame,
        page,
        timeout_ms=_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS,
        settle_ms=80,
    )
    _dismiss_terminal_dialog_once(lab_frame=lab_frame, page=page, settle_ms=120)
    update_trace_summary(terminal_transport="dom_fallback_terminal")
    trace_event("terminal_open_ready", source="dom_fallback", term_name=term_name)
    return True, term_name


# ---------------------------------------------------------------------------
# Phase 2: Read-only WS output listener for hybrid browser fallback
# ---------------------------------------------------------------------------

_WS_CAPTURE_BUF_CAP = 8000


def _attach_ws_output_listener(
    page_or_frame: Any,
    *,
    ws_url: str,
    completion_marker: str,
    error_markers: list[str],
) -> bool:
    """Attach a read-only WebSocket listener for stdout marker detection.

    Sets up ``window._inspireWsCapture`` (state dict) and
    ``window._inspireWsCaptureSocket`` (WebSocket ref) on the target frame.
    The internal stdout buffer is used for marker matching only — it is never
    exposed to Python to avoid leaking sensitive setup output (e.g. SSH keys).

    Returns ``True`` if the JS setup succeeded.
    """
    try:
        result = page_or_frame.evaluate(
            """
            ({ wsUrl, completionMarker, errorMarkers, bufCap }) => {
              try {
                window._inspireWsCapture = {
                  done: false, errors: [], markerFound: false,
                  wsConnected: false, stdoutReceived: false, stdoutLen: 0,
                  wsCloseCode: null, wsCloseReason: "", elapsed: 0
                };
                const state = window._inspireWsCapture;
                const startTime = Date.now();
                let buf = "";

                const socket = new WebSocket(wsUrl);
                window._inspireWsCaptureSocket = socket;

                const finish = () => {
                  state.elapsed = Date.now() - startTime;
                  state.done = true;
                  try { socket.close(); } catch (_) {}
                };

                socket.addEventListener("open", () => {
                  state.wsConnected = true;
                });

                socket.addEventListener("message", (ev) => {
                  try {
                    const msg = JSON.parse(ev.data);
                    if (Array.isArray(msg) && msg[0] === "stdout") {
                      const text = String(msg[1]);
                      state.stdoutReceived = true;
                      buf += text;
                      if (buf.length > bufCap) buf = buf.slice(-bufCap);
                      state.stdoutLen = buf.length;

                      for (const em of errorMarkers) {
                        if (em && buf.includes(em) && !state.errors.includes(em)) {
                          state.errors.push(em);
                        }
                      }
                      if (state.errors.length > 0) {
                        finish();
                        return;
                      }
                      if (completionMarker && buf.includes(completionMarker)) {
                        state.markerFound = true;
                        finish();
                      }
                    }
                  } catch (_) {}
                });

                socket.addEventListener("close", (ev) => {
                  state.wsCloseCode = ev.code;
                  state.wsCloseReason = ev.reason || "";
                  if (!state.done) finish();
                });

                socket.addEventListener("error", () => {
                  if (!state.done) finish();
                });

                return true;
              } catch (_) {
                return false;
              }
            }
            """,
            {
                "wsUrl": ws_url,
                "completionMarker": completion_marker,
                "errorMarkers": error_markers,
                "bufCap": _WS_CAPTURE_BUF_CAP,
            },
        )
        trace_event("terminal_ws_listener_attach_result", ok=bool(result))
        return bool(result)
    except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError) as exc:
        trace_event("terminal_ws_listener_attach_exception", error=exc)
        return False


def _poll_ws_capture(page_or_frame: Any) -> dict:
    """Read the current state of the WS output listener.

    Returns a safe default dict on any error so callers never need to
    handle ``None``.
    """
    try:
        result = page_or_frame.evaluate("window._inspireWsCapture || null")
        if isinstance(result, dict):
            return result
    except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return {
        "done": False,
        "errors": [],
        "markerFound": False,
        "wsConnected": False,
        "stdoutReceived": False,
        "stdoutLen": 0,
        "wsCloseCode": None,
        "wsCloseReason": "",
        "elapsed": 0,
    }


def _wait_for_ws_capture(
    page_or_frame: Any,
    page: Any,
    *,
    timeout_ms: int,
    poll_interval_ms: int = 500,
) -> dict:
    """Poll ``_poll_ws_capture`` until done, errors found, or timeout.

    Uses ``page.wait_for_timeout`` for Playwright-friendly sleeping.
    Returns the final capture state dict.
    """
    start = time.monotonic()
    while True:
        state = _poll_ws_capture(page_or_frame)
        if state.get("done") or state.get("errors"):
            trace_event(
                "terminal_ws_capture_done",
                marker_found=state.get("markerFound"),
                errors=",".join(state.get("errors", [])),
                ws_connected=state.get("wsConnected"),
                stdout_received=state.get("stdoutReceived"),
            )
            return state
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if elapsed_ms >= timeout_ms:
            trace_event("terminal_ws_capture_timeout", elapsed_ms=elapsed_ms)
            return state
        remaining = timeout_ms - elapsed_ms
        sleep_ms = min(poll_interval_ms, remaining)
        if sleep_ms <= 0:
            return state
        page.wait_for_timeout(sleep_ms)


def _detach_ws_output_listener(page_or_frame: Any) -> None:
    """Close the WS capture socket and clean up window globals.  Best-effort."""
    try:
        page_or_frame.evaluate(
            """
            () => {
              try {
                if (window._inspireWsCaptureSocket) {
                  window._inspireWsCaptureSocket.close();
                }
              } catch (_) {}
              delete window._inspireWsCapture;
              delete window._inspireWsCaptureSocket;
            }
            """
        )
        trace_event("terminal_ws_listener_detached")
    except Exception as exc:
        trace_event("terminal_ws_listener_detach_failed", error=exc)
        pass
