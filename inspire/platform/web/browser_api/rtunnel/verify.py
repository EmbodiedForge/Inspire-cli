"""Proxy-readiness verification: URL redaction and lightweight HTTP probing."""

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
            redacted_items = []
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


def probe_rtunnel_proxy_once(
    *,
    proxy_url: str,
    context: Any,
    request_timeout_ms: int = 5000,
) -> tuple[bool, str]:
    """Run a single HTTP probe against an rtunnel proxy URL.

    Returns ``(ready, summary)`` where *ready* reflects the current HTTP response
    and *summary* is a single-line, redacted status suitable for debug logs.
    """
    display_url = redact_proxy_url(proxy_url)
    trace_event("proxy_probe_once_start", proxy_url=display_url, timeout_ms=request_timeout_ms)
    try:
        resp = context.request.get(proxy_url, timeout=request_timeout_ms)
        try:
            body = resp.text()
        except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError):
            body = ""
        summary = _redact_token_like_text(f"{resp.status} {body[:200].strip()}")
        ready = _is_rtunnel_proxy_ready(status=resp.status, body=body)
        trace_event(
            "proxy_probe_once_result",
            proxy_url=display_url,
            ready=ready,
            status=summary,
        )
        return ready, summary
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ) as exc:
        summary = _summarize_request_error(exc)
        trace_event("proxy_probe_once_error", proxy_url=display_url, error=summary)
        return False, summary


def wait_for_rtunnel_reachable(
    *,
    proxy_url: str,
    timeout_s: int,
    context: Any,
    page: Any,
) -> None:
    """Wait until rtunnel becomes reachable via the notebook proxy URL, or raise ValueError."""
    import sys as _sys

    display_url = redact_proxy_url(proxy_url)
    trace_event("proxy_poll_start", proxy_url=display_url, timeout_s=timeout_s)
    _sys.stderr.write(f"  Polling proxy URL: {display_url}\n")
    _sys.stderr.flush()

    start = time.time()
    last_status = None
    last_progress_time = start
    attempt = 0
    consecutive_404 = 0
    while time.time() - start < timeout_s:
        attempt += 1
        elapsed = time.time() - start
        if time.time() - last_progress_time >= 30:
            _sys.stderr.write(f"  Waiting for rtunnel... ({int(elapsed)}s elapsed)\n")
            _sys.stderr.flush()
            last_progress_time = time.time()
        try:
            resp = context.request.get(proxy_url, timeout=5000)
            try:
                body = resp.text()
            except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError):
                body = ""
            last_status = _redact_token_like_text(f"{resp.status} {body[:200].strip()}")
            if attempt <= 3:
                _sys.stderr.write(f"  Attempt {attempt}: {last_status}\n")
                _sys.stderr.flush()
            if attempt <= 5:
                trace_event("proxy_poll_attempt", attempt=attempt, status=last_status)
            if _is_rtunnel_proxy_ready(status=resp.status, body=body):
                trace_event("proxy_poll_ready", attempt=attempt, status=last_status)
                return
            text = (body or "").strip().lower()
            if resp.status == 404 and "page not found" in text and "<html" not in text:
                consecutive_404 += 1
            else:
                consecutive_404 = 0
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
                _sys.stderr.write(f"  Attempt {attempt}: {last_status}\n")
                _sys.stderr.flush()
            if attempt <= 5:
                trace_event("proxy_poll_attempt_error", attempt=attempt, error=last_status)

        if consecutive_404 >= 3 and (time.time() - start) >= 2:
            trace_event(
                "proxy_poll_plain_text_404",
                consecutive_404=consecutive_404,
                elapsed=int(time.time() - start),
            )
            raise ValueError(
                f"rtunnel server returned plain-text 404 on {consecutive_404} "
                f"consecutive attempts ({int(time.time() - start)}s elapsed).\n"
                f"Proxy URL: {display_url}\n"
                f"Last response: {last_status}"
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
        "  6. Screenshot saved to /tmp/notebook_terminal_debug.png"
    )
    trace_event("proxy_poll_timeout", timeout_s=timeout_s, last_status=last_status)
    raise ValueError(error_msg)
