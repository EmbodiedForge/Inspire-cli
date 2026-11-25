"""Proxy-readiness verification: URL redaction and WebSocket probing."""

from __future__ import annotations

import re
import time
from typing import Any

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        pass


import logging
from http.client import responses as http_status_reasons
from urllib.parse import urlsplit, urlunsplit

import websocket

from inspire.platform.tls_policy import ssl_options

from .logging import trace_event

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel")


def redact_proxy_url(proxy_url: str) -> str:
    """Redact sensitive tokens from a notebook proxy URL for logs/errors.

    Proxy URLs may contain tokens either as a path segment:
      /jupyter/<notebook>/<token>/proxy/<port>/
    or as a query parameter:
      .../proxy/<port>/?token=<token>
    """
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return proxy_url

    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(proxy_url)
        path_segments = parts.path.split("/")

        for marker in ("jupyter", "vscode"):
            for idx, seg in enumerate(path_segments):
                if seg != marker:
                    continue
                # /<marker>/<notebook>/<token>/proxy/<port>/ -> token is idx+2
                if idx + 3 < len(path_segments) and path_segments[idx + 3] == "proxy":
                    if idx + 2 < len(path_segments) and path_segments[idx + 2]:
                        path_segments[idx + 2] = "<redacted>"

        redacted_path = "/".join(path_segments)

        if parts.query:
            query_items = parse_qsl(parts.query, keep_blank_values=True)
            redacted_items: list[tuple[str, str]] = []
            for key, value in query_items:
                if key.lower() in {"token", "access_token"}:
                    redacted_items.append((key, "<redacted>" if value else value))
                else:
                    redacted_items.append((key, value))
            redacted_query = urlencode(redacted_items)
        else:
            redacted_query = parts.query

        return urlunsplit(
            (parts.scheme, parts.netloc, redacted_path, redacted_query, parts.fragment)
        )
    except (ValueError, TypeError, AttributeError):
        # Best-effort fallback: redact obvious token query patterns.
        if "token=" in proxy_url:
            before, _, after = proxy_url.partition("token=")
            if "&" in after:
                _token, _, rest = after.partition("&")
                return before + "token=<redacted>&" + rest
            return before + "token=<redacted>"
        return proxy_url


def _is_rtunnel_proxy_ready(*, status: int, body: str) -> bool:
    text = (body or "").strip().lower()

    if status == 200:
        if not text:
            return True
        if (
            "econnrefused" in text
            or "connection refused" in text
            or "404 page not found" in text
            or "<html" in text
            or "<!doctype html" in text
            or "jupyter server" in text
        ):
            return False
        return True

    return False


_TOKEN_QUERY_RE = re.compile(r"(?i)(token=)[^\s&'\"]+")
_TOKEN_PATH_RE = re.compile(r"(/(?:jupyter|vscode)/[^/]+/)([^/]+)(/proxy/)")


def _redact_token_like_text(text: str) -> str:
    value = str(text or "")
    if not value:
        return value

    value = _TOKEN_QUERY_RE.sub(r"\1<redacted>", value)
    value = _TOKEN_PATH_RE.sub(r"\1<redacted>\3", value)
    return value


def _summarize_request_error(error: Exception) -> str:
    """Return a safe single-line summary for Playwright request errors."""
    message = str(error).strip()
    if not message:
        return error.__class__.__name__
    headline = message.splitlines()[0].strip()
    return _redact_token_like_text(headline)


def _proxy_websocket_url(proxy_url: str) -> str:
    parts = urlsplit(proxy_url)
    if parts.scheme == "https":
        scheme = "wss"
    elif parts.scheme == "http":
        scheme = "ws"
    elif parts.scheme in {"ws", "wss"}:
        scheme = parts.scheme
    else:
        raise ValueError(f"Unsupported notebook proxy URL scheme: {parts.scheme or '<empty>'}")
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _proxy_cookie_header(*, proxy_url: str, context: Any = None, session: Any = None) -> str:
    if session is not None and hasattr(session, "prepare_request"):
        try:
            import requests

            prepared = session.prepare_request(requests.Request("GET", proxy_url))
            return str(prepared.headers.get("Cookie") or "")
        except Exception:
            pass

    cookies: list[Any] = []
    try:
        if context is not None:
            cookies = list(context.cookies([proxy_url]) or [])
        elif session is not None:
            cookies = list(getattr(session, "cookies", []) or [])
    except Exception:
        cookies = []

    items: list[str] = []
    for cookie in cookies:
        if isinstance(cookie, dict):
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
        else:
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
        if not name or not value:
            continue
        items.append(f"{name}={value}")
    return "; ".join(items)


def probe_rtunnel_proxy_once(
    *,
    proxy_url: str,
    context: Any = None,
    request_timeout_ms: int = 5000,
    session: Any = None,
) -> tuple[bool, str]:
    """Try the WebSocket upgrade that SSH will actually depend on.

    A plain HTTP GET is ambiguous: a healthy rtunnel endpoint commonly returns
    404 until the request includes WebSocket upgrade headers.  Returns
    ``(ready, summary)`` with a redacted single-line diagnostic.
    """
    display_url = redact_proxy_url(proxy_url)
    trace_event("proxy_probe_once_start", proxy_url=display_url, timeout_ms=request_timeout_ms)
    ws = None
    try:
        ws_url = _proxy_websocket_url(proxy_url)
        cookie = _proxy_cookie_header(
            proxy_url=proxy_url,
            context=context,
            session=session,
        )
        options: dict[str, Any] = {
            "timeout": max(request_timeout_ms, 1) / 1000.0,
            "enable_multithread": False,
            # The probe handshakes with the session cookie too, so it follows the same policy as
            # the tunnel it is checking.
            "sslopt": ssl_options(),
        }
        if cookie:
            options["cookie"] = cookie
        ws = websocket.create_connection(ws_url, **options)
        summary = "websocket 101 Switching Protocols"
        trace_event(
            "proxy_probe_once_result",
            proxy_url=display_url,
            ready=True,
            status=summary,
        )
        return True, summary
    except websocket.WebSocketBadStatusException as exc:
        status = int(getattr(exc, "status_code", 0) or 0)
        reason = http_status_reasons.get(status, "HTTP error") if status else "HTTP error"
        summary = f"websocket {status or 'rejected'} {reason}"
        trace_event("proxy_probe_once_result", proxy_url=display_url, ready=False, status=summary)
        return False, summary
    except Exception as exc:
        summary = _summarize_request_error(exc)
        trace_event("proxy_probe_once_error", proxy_url=display_url, error=summary)
        return False, summary
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def wait_for_rtunnel_reachable(
    *,
    proxy_url: str,
    timeout_s: int,
    context: Any,
    page: Any,
) -> None:
    """Wait until rtunnel becomes reachable via the notebook proxy URL, or raise ValueError."""
    display_url = redact_proxy_url(proxy_url)
    trace_event("proxy_poll_start", proxy_url=display_url, timeout_s=timeout_s)
    _log.debug("  Polling proxy URL: %s", display_url)

    start = time.time()
    last_status = None
    last_progress_time = start
    attempt = 0
    while time.time() - start < timeout_s:
        attempt += 1
        elapsed = time.time() - start
        if time.time() - last_progress_time >= 30:
            _log.info("  Waiting for rtunnel... (%ds elapsed)", int(elapsed))
            last_progress_time = time.time()
        try:
            ready, last_status = probe_rtunnel_proxy_once(
                proxy_url=proxy_url,
                context=context,
                request_timeout_ms=5000,
            )
            if attempt <= 3:
                _log.debug("  Attempt %d: %s", attempt, last_status)
            if attempt <= 5:
                trace_event("proxy_poll_attempt", attempt=attempt, status=last_status)
            if ready:
                trace_event("proxy_poll_ready", attempt=attempt, status=last_status)
                return
        except (
            PlaywrightError,
            ConnectionError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as e:
            last_status = _summarize_request_error(e)
            if attempt <= 3:
                _log.debug("  Attempt %d: %s", attempt, last_status)
            if attempt <= 5:
                trace_event("proxy_poll_attempt_error", attempt=attempt, error=last_status)

        if last_status and "websocket 403 " in last_status.lower():
            trace_event(
                "proxy_poll_platform_denied",
                elapsed=int(time.time() - start),
            )
            raise ValueError(
                "Notebook ingress denied the rtunnel WebSocket upgrade (HTTP 403).\n"
                f"Proxy URL: {display_url}\n"
                f"Last response: {last_status}\n"
                "The SSH server inside the notebook may still be healthy; interactive SSH "
                "requires the platform proxy policy to allow this route."
            )

        elapsed = time.time() - start
        if elapsed < 3:
            poll_ms = 180
        elif elapsed < 8:
            poll_ms = 300
        elif elapsed < 20:
            poll_ms = 650
        else:
            poll_ms = 1000
        page.wait_for_timeout(poll_ms)

    error_msg = (
        f"rtunnel server did not become reachable within {timeout_s}s.\n"
        f"Proxy URL: {display_url}\n"
        f"Last response: {last_status}\n\n"
        "Debugging hints:\n"
        "  1. Check if rtunnel binary is present: ls -la /tmp/rtunnel\n"
        "  2. Check rtunnel server log: cat /tmp/rtunnel-server.log\n"
        "  3. Check if sshd/dropbear is running: ps aux | grep -E 'sshd|dropbear'\n"
        "  4. Check dropbear log: cat /tmp/dropbear.log\n"
        "  5. Try running with --debug-playwright to see the browser\n"
        "  6. If captured, the screenshot path is shown as screenshot_path in the RTunnel trace summary"
    )
    trace_event("proxy_poll_timeout", timeout_s=timeout_s, last_status=last_status)
    raise ValueError(error_msg)
