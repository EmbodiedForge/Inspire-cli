"""Verification helpers for notebook rtunnel reachability."""

from __future__ import annotations

import time
from typing import Any


def wait_for_rtunnel_reachable(
    *,
    proxy_url: str,
    timeout_s: int,
    context: Any,
    page: Any,
) -> None:
    """Wait until rtunnel becomes reachable via the notebook proxy URL, or raise ValueError."""
    import sys as _sys

    _sys.stderr.write(f"  Polling proxy URL: {proxy_url}\n")
    _sys.stderr.flush()

    start = time.time()
    last_status = None
    last_progress_time = start
    attempt = 0
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
            except Exception:
                body = ""
            last_status = f"{resp.status} {body[:200].strip()}"
            if attempt <= 3:
                _sys.stderr.write(f"  Attempt {attempt}: {last_status}\n")
                _sys.stderr.flush()
            if "ECONNREFUSED" not in body:
                return
        except Exception as e:
            last_status = str(e)
            if attempt <= 3:
                _sys.stderr.write(f"  Attempt {attempt}: {last_status}\n")
                _sys.stderr.flush()

        elapsed = time.time() - start
        if elapsed < 5:
            poll_ms = 300
        elif elapsed < 20:
            poll_ms = 700
        else:
            poll_ms = 1000
        page.wait_for_timeout(poll_ms)

    error_msg = (
        f"rtunnel server did not become reachable within {timeout_s}s.\n"
        f"Proxy URL: {proxy_url}\n"
        f"Last response: {last_status}\n\n"
        "Debugging hints:\n"
        "  1. Check if rtunnel binary is present: ls -la /tmp/rtunnel\n"
        "  2. Check rtunnel server log: cat /tmp/rtunnel-server.log\n"
        "  3. Check if sshd/dropbear is running: ps aux | grep -E 'sshd|dropbear'\n"
        "  4. Check dropbear log: cat /tmp/dropbear.log\n"
        "  5. Try running with --debug-playwright to see the browser\n"
        "  6. Screenshot saved to /tmp/notebook_terminal_debug.png"
    )
    raise ValueError(error_msg)
