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
    start = time.time()
    last_status = None
    last_progress_time = start
    while time.time() - start < timeout_s:
        elapsed = time.time() - start
        if time.time() - last_progress_time >= 30:
            import sys as _sys

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
            if "ECONNREFUSED" not in body:
                return
        except Exception as e:
            last_status = str(e)

        page.wait_for_timeout(1000)

    error_msg = (
        f"rtunnel server did not become reachable within {timeout_s}s.\n"
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
