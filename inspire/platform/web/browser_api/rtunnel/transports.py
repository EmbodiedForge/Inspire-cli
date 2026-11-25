"""Transport protocol / abstraction for notebook terminal interactions."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import websocket

from inspire.platform.tls_policy import ssl_options
from inspire.platform.web.browser_api.rtunnel._jupyter import (
    _jupyter_server_base,
)
from inspire.platform.web.browser_api.rtunnel.commands import (
    SSHD_MISSING_MARKER,
    SSH_SERVER_MISSING_MARKER,
    STEP_FAILED_PREFIX,
)
from inspire.platform.web.browser_api.rtunnel.logging import trace_event, update_trace_summary
from inspire.platform.web.browser_api.rtunnel.terminal import (
    _build_terminal_websocket_url,
    _create_terminal_via_api,
    _delete_terminal_via_api,
    _send_terminal_command_via_websocket,
)
from inspire.platform.web.session import WebSession
from inspire.platform.web.session.requests import (
    build_requests_session,
    cookie_header_for_url,
)

import logging

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel.transports")

# Match the JS implementation chunking constants from terminal.py
_TERMINAL_STDIN_CHUNK = 512
_TERMINAL_STDIN_DELAY_MS = 20
_STDOUT_TAIL_CAP = 1024 * 1024
_SETUP_MARKER_LINE_CAP = 262144
_SETUP_MARKER_PLACEHOLDER = f"{STEP_FAILED_PREFIX}<omitted>"

#: Why ``send_command`` stopped reading, published under ``diagnostics["exitReason"]``.
#:
#: The distinction this exists for is ``deadline`` from ``channel-closed``: the first means our own
#: clock ran out, the second means the terminal stopped answering before it did. Both used to end the
#: read loop the same silent way, so the caller could only report a timeout for both and advise the
#: one remedy (raise --timeout) that cannot help a dead channel.
EXIT_NO_TERMINAL = "no-terminal"
EXIT_CONNECT_FAILED = "connect-failed"
EXIT_SEND_FAILED = "send-failed"
EXIT_CHANNEL_CLOSED = "channel-closed"
EXIT_DEADLINE = "deadline"
EXIT_MARKER = "marker"
#: The DOM transport's "command delivered, nothing to wait for" - it settles ok without a marker.
EXIT_SENT = "sent"
EXIT_REASONS: tuple[str, ...] = (
    EXIT_NO_TERMINAL,
    EXIT_CONNECT_FAILED,
    EXIT_SEND_FAILED,
    EXIT_CHANNEL_CLOSED,
    EXIT_DEADLINE,
    EXIT_MARKER,
    EXIT_SENT,
)


def _scan_setup_markers(
    text: str,
    *,
    current_line: str,
    errors: list[str],
) -> str:
    """Scan raw stdout for setup markers without depending on the diagnostic tail."""
    value = str(text or "")
    for marker in (SSHD_MISSING_MARKER, SSH_SERVER_MISSING_MARKER):
        if marker in value and marker not in errors:
            errors.append(marker)

    combined = current_line + value
    parts = combined.split("\n")
    if combined.endswith("\n"):
        complete_lines = parts[:-1]
        next_line = ""
    else:
        complete_lines = parts[:-1]
        next_line = parts[-1]

    for line in complete_lines:
        item = line.strip()
        if item.startswith(STEP_FAILED_PREFIX) and item not in errors:
            errors.append(item)

    if len(next_line) > _SETUP_MARKER_LINE_CAP:
        if next_line.strip().startswith(STEP_FAILED_PREFIX):
            return next_line[:_SETUP_MARKER_LINE_CAP]
        return next_line[-_SETUP_MARKER_LINE_CAP:]
    return next_line


def _append_setup_stdout_tail(
    tail: str,
    text: str,
    *,
    current_line: str,
    cap: int = _STDOUT_TAIL_CAP,
) -> tuple[str, str]:
    """Append stdout to a bounded diagnostic tail while omitting encoded markers."""
    combined = current_line + str(text or "")
    parts = combined.split("\n")
    if combined.endswith("\n"):
        complete_lines = parts[:-1]
        next_line = ""
    else:
        complete_lines = parts[:-1]
        next_line = parts[-1]

    for line in complete_lines:
        if line.strip().startswith(STEP_FAILED_PREFIX):
            tail += _SETUP_MARKER_PLACEHOLDER + "\n"
        else:
            tail += line + "\n"
        if len(tail) > cap:
            tail = tail[-cap:]

    if len(next_line) > _SETUP_MARKER_LINE_CAP:
        if next_line.strip().startswith(STEP_FAILED_PREFIX):
            next_line = next_line[:_SETUP_MARKER_LINE_CAP]
        else:
            next_line = next_line[-_SETUP_MARKER_LINE_CAP:]
    return tail, next_line


class TerminalTransport(Protocol):
    """Protocol for terminal transports (DOM-based or native WS)."""

    def create_terminal(self) -> str | None:
        """Create a terminal and return its name, or None on failure."""
        ...

    def send_command(self, cmd: str, marker: str, timeout: int) -> dict:
        """Send *cmd* and wait for *marker* (or *timeout* seconds).

        Returns a diagnostics dict with at least the keys:
        ``wsConnected``, ``promptDetected``, ``commandSent``,
        ``stdoutReceived``, ``stdoutLen``, ``exitReason``.
        """
        ...

    def cleanup(self) -> None:
        """Best-effort cleanup (delete terminal, close sockets, etc.)."""
        ...


class PlaywrightDomTransport:
    """Existing DOM-based transport wrapping terminal.py helpers."""

    def __init__(self, context: Any, page: Any, lab_frame: Any) -> None:
        self.context = context
        self.page = page
        self.lab_frame = lab_frame
        self.lab_url: str = getattr(lab_frame, "url", "")
        self._term_name: str | None = None

    def create_terminal(self) -> str | None:
        self._term_name = _create_terminal_via_api(self.context, self.lab_url)
        trace_event("transport_pw_create_terminal", term_name=self._term_name)
        return self._term_name

    def send_command(self, cmd: str, marker: str, timeout: int) -> dict:
        if not self._term_name:
            return {
                "wsConnected": False,
                "promptDetected": False,
                "commandSent": False,
                "stdoutReceived": False,
                "stdoutLen": 0,
                "exitReason": EXIT_NO_TERMINAL,
            }

        ws_url = _build_terminal_websocket_url(self.lab_url, self._term_name)
        timeout_ms = max(int(timeout * 1000), 1000)
        diagnostics: dict[str, Any] = {}

        ok = _send_terminal_command_via_websocket(
            self.lab_frame,
            ws_url=ws_url,
            command=cmd,
            timeout_ms=timeout_ms,
            completion_marker=marker,
            diagnostics_out=diagnostics,
        )
        update_trace_summary(terminal_transport="playwright_dom")
        # The JS side already answers "how did the socket end?" (wsCloseCode/wsCloseReason), so the
        # same exitReason the WS transport publishes is derivable here rather than guessed at.
        close_code = diagnostics.get("wsCloseCode")
        if not diagnostics:
            # The browser call itself raised, so no socket state was ever reported.
            exit_reason = EXIT_SEND_FAILED
        elif ok:
            exit_reason = EXIT_MARKER if marker else EXIT_SENT
        elif close_code is not None:
            exit_reason = EXIT_CHANNEL_CLOSED
            diagnostics["channelError"] = "websocket closed: code {0} {1}".format(
                close_code, str(diagnostics.get("wsCloseReason") or "").strip()
            )
        elif not diagnostics.get("wsConnected"):
            exit_reason = EXIT_CONNECT_FAILED
        else:
            exit_reason = EXIT_DEADLINE
        diagnostics["exitReason"] = exit_reason
        elapsed = diagnostics.get("elapsed")
        if elapsed is not None:
            diagnostics.setdefault("elapsed_ms", int(elapsed))
        return {
            "wsConnected": diagnostics.get("wsConnected", False),
            "promptDetected": diagnostics.get("promptDetected", False),
            "commandSent": diagnostics.get("commandSent", False),
            "stdoutReceived": diagnostics.get("stdoutReceived", False),
            "stdoutLen": diagnostics.get("stdoutLen", 0),
            "stdoutTail": diagnostics.get("stdoutTail", ""),
            "stdout": diagnostics.get("stdoutTail", ""),
            "errors": diagnostics.get("errors", []),
            "ok": ok,
            **{k: v for k, v in diagnostics.items() if k not in {"ok", "errors"}},
        }

    def cleanup(self) -> None:
        if self._term_name:
            _delete_terminal_via_api(self.context, lab_url=self.lab_url, term_name=self._term_name)
            trace_event("transport_pw_cleanup", term_name=self._term_name)
            self._term_name = None


class PythonWsTransport:
    """New Python-native WebSocket transport using requests + websocket-client."""

    def __init__(self, session: WebSession, lab_url: str, token: str | None = None) -> None:
        self.session = session
        self.lab_url = lab_url
        self.base_url = _jupyter_server_base(lab_url)
        self._term_name: str | None = None
        self._http = build_requests_session(session, self.base_url)
        self._ws: websocket.WebSocket | None = None
        self._token = token

    def create_terminal(self) -> str | None:
        api_url = f"{self.base_url}api/terminals"
        headers = self._auth_headers()
        try:
            resp = self._http.post(api_url, headers=headers, timeout=10)
            trace_event("transport_py_create_terminal", status=resp.status_code)
            if resp.status_code in (200, 201):
                data = resp.json()
                self._term_name = data.get("name")
                return self._term_name
        except Exception as exc:
            trace_event("transport_py_create_terminal_failed", error=str(exc))
        return None

    def send_command(self, cmd: str, marker: str, timeout: int) -> dict:
        if not self._term_name:
            return {
                "wsConnected": False,
                "promptDetected": False,
                "commandSent": False,
                "stdoutReceived": False,
                "stdoutLen": 0,
                "exitReason": EXIT_NO_TERMINAL,
            }

        ws_url = _build_terminal_websocket_url(self.lab_url, self._term_name)
        timeout_ms = max(int(timeout * 1000), 1000)
        start = time.monotonic()

        diagnostics: dict[str, Any] = {
            "wsConnected": False,
            "promptDetected": False,
            "commandSent": False,
            "stdoutReceived": False,
            "stdoutLen": 0,
            "stdoutTail": "",
            "stdoutTotalLen": 0,
            "stdoutTruncated": False,
            "errors": [],
        }
        # Why the read loop ended. `deadline` is the default because the loop condition is the
        # deadline; every other way out sets it explicitly.
        exit_reason = EXIT_DEADLINE

        try:
            self._ws = websocket.create_connection(
                ws_url,
                timeout=timeout,
                cookie=cookie_header_for_url(self._http, ws_url),
                sslopt=ssl_options(),
            )
            diagnostics["wsConnected"] = True
        except Exception as exc:
            trace_event("transport_py_ws_connect_failed", error=str(exc))
            diagnostics["exitReason"] = EXIT_CONNECT_FAILED
            diagnostics["channelError"] = f"{type(exc).__name__}: {exc}"
            diagnostics["elapsed_ms"] = int((time.monotonic() - start) * 1000)
            diagnostics["stdout"] = diagnostics.get("stdoutTail", "")
            return diagnostics

        try:
            # Wait for prompt (best-effort) or fall back to a short delay
            prompt_found = self._wait_for_prompt(timeout=3)
            diagnostics["promptDetected"] = prompt_found

            self._send_stdin_chunks(cmd.rstrip("\r\n") + "\r")
            diagnostics["commandSent"] = True

            stdout_tail = ""
            raw_marker_line = ""
            tail_line = ""
            deadline = start + (timeout_ms / 1000.0)
            while time.monotonic() < deadline:
                try:
                    self._ws.settimeout(0.5)
                    raw = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as exc:
                    # The socket stopped answering: closed, reset, or unreadable. This is not our
                    # clock running out, and the difference is the whole report (issue: a dead
                    # channel was reported as "did not finish within {timeout}s").
                    exit_reason = EXIT_CHANNEL_CLOSED
                    diagnostics["channelError"] = f"{type(exc).__name__}: {exc}"
                    trace_event(
                        "transport_py_ws_channel_closed",
                        error=str(exc),
                        elapsed_ms=int((time.monotonic() - start) * 1000),
                        stdoutLen=diagnostics["stdoutLen"],
                    )
                    break

                if not raw:
                    continue

                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue

                if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "stdout":
                    text = str(msg[1])
                    diagnostics["stdoutReceived"] = True
                    diagnostics["stdoutTotalLen"] += len(text)
                    if diagnostics["stdoutTotalLen"] > _STDOUT_TAIL_CAP:
                        diagnostics["stdoutTruncated"] = True
                    raw_marker_line = _scan_setup_markers(
                        text,
                        current_line=raw_marker_line,
                        errors=diagnostics["errors"],
                    )
                    stdout_tail, tail_line = _append_setup_stdout_tail(
                        stdout_tail,
                        text,
                        current_line=tail_line,
                    )
                    display_tail = stdout_tail + (
                        _SETUP_MARKER_PLACEHOLDER
                        if tail_line.strip().startswith(STEP_FAILED_PREFIX)
                        else tail_line
                    )
                    if len(display_tail) > _STDOUT_TAIL_CAP:
                        display_tail = display_tail[-_STDOUT_TAIL_CAP:]
                    diagnostics["stdoutLen"] = len(display_tail)
                    diagnostics["stdoutTail"] = display_tail

                    if marker and (marker in text or marker in display_tail):
                        diagnostics["markerFound"] = True
                        exit_reason = EXIT_MARKER
                        break

            update_trace_summary(terminal_transport="python_ws")
        except Exception as exc:
            exit_reason = EXIT_SEND_FAILED
            diagnostics["channelError"] = f"{type(exc).__name__}: {exc}"
            trace_event("transport_py_ws_exception", error=str(exc))
        finally:
            self._close_ws()
            diagnostics["exitReason"] = exit_reason

        diagnostics["elapsed_ms"] = int((time.monotonic() - start) * 1000)
        diagnostics["stdout"] = diagnostics.get("stdoutTail", "")
        return diagnostics

    def cleanup(self) -> None:
        self._close_ws()
        if self._term_name:
            api_url = f"{self.base_url}api/terminals/{self._term_name}"
            headers = self._auth_headers()
            try:
                resp = self._http.delete(api_url, headers=headers, timeout=5)
                trace_event(
                    "transport_py_cleanup",
                    term_name=self._term_name,
                    status=resp.status_code,
                )
            except Exception as exc:
                trace_event(
                    "transport_py_cleanup_failed", term_name=self._term_name, error=str(exc)
                )
            self._term_name = None

    # ── internals ────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        """Return headers for Jupyter API auth.

        Prefer ``Authorization: token <token>`` when a token is available,
        otherwise fall back to the ``_xsrf`` cookie.
        """
        if self._token:
            return {"Authorization": f"token {self._token}"}
        return self._xsrf_headers()

    def _xsrf_headers(self) -> dict[str, str]:
        """Return headers dict with X-XSRFToken when the cookie is present."""
        xsrf = None
        for cookie in self._http.cookies:
            if cookie.name == "_xsrf":
                xsrf = cookie.value
                break
        if xsrf:
            return {"X-XSRFToken": xsrf}
        return {}

    def _send_stdin_chunks(self, data: str) -> None:
        """Chunk *data* into 512-byte pieces with a 20 ms delay (matches JS)."""
        if not self._ws:
            return
        chunks = [
            data[i : i + _TERMINAL_STDIN_CHUNK] for i in range(0, len(data), _TERMINAL_STDIN_CHUNK)
        ]
        for idx, chunk in enumerate(chunks):
            self._ws.send(json.dumps(["stdin", chunk]))
            if idx < len(chunks) - 1:
                time.sleep(_TERMINAL_STDIN_DELAY_MS / 1000.0)

    def _wait_for_prompt(self, timeout: float = 3.0) -> bool:
        """Best-effort prompt detection before sending stdin."""
        if not self._ws:
            return False
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            try:
                self._ws.settimeout(0.2)
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            finally:
                try:
                    self._ws.settimeout(None)
                except Exception:
                    pass

            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "stdout":
                buf += str(msg[1])
                if buf.rstrip().endswith(("$", "#")):
                    return True
        return False

    def _close_ws(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
