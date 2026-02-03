"""Web session management for web UI APIs.

This module keeps the historical import path stable while splitting the implementation into
smaller, testable modules under `inspire.cli.utils._impl.web_session`.
"""

from __future__ import annotations

import atexit
from typing import Callable, Optional

from inspire.cli.utils._impl.web_session.browser_client import _BrowserRequestClient  # noqa: F401
from inspire.cli.utils._impl.web_session.browser_client import (
    _close_browser_client,
    _get_browser_client,
)
from inspire.cli.utils._impl.web_session.auth import (
    get_credentials as _get_credentials,
    get_web_session as _get_web_session,
    login_with_playwright as _login_with_playwright,
)
from inspire.cli.utils._impl.web_session.models import (
    DEFAULT_WORKSPACE_ID,
    SESSION_CACHE_FILE,
    SESSION_TTL,
    SessionExpiredError,
    WebSession,
)
from inspire.cli.utils._impl.web_session.proxy import get_playwright_proxy
from inspire.cli.utils._impl.web_session.requests import build_requests_session
from inspire.cli.utils._impl.web_session.workspace import (
    GPUAvailability,
    fetch_gpu_availability as _fetch_gpu_availability,
    fetch_node_specs as _fetch_node_specs,
    fetch_workspace_availability as _fetch_workspace_availability,
)

__all__ = [
    "DEFAULT_WORKSPACE_ID",
    "GPUAvailability",
    "SESSION_CACHE_FILE",
    "SESSION_TTL",
    "SessionExpiredError",
    "WebSession",
    "build_requests_session",
    "clear_session_cache",
    "fetch_gpu_availability",
    "fetch_node_specs",
    "fetch_workspace_availability",
    "get_credentials",
    "get_playwright_proxy",
    "get_web_session",
    "login_with_playwright",
    "request_json",
]


_BROWSER_API_FORCE_BROWSER = False


atexit.register(_close_browser_client)


def request_json(
    session: "WebSession",
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict] = None,
    timeout: int = 30,
    _retry_count: int = 0,
) -> dict:
    global _BROWSER_API_FORCE_BROWSER

    if not _BROWSER_API_FORCE_BROWSER:
        http = build_requests_session(session, url)
        try:
            method_upper = method.upper()
            req_headers = headers or {}
            if method_upper == "GET":
                resp = http.get(url, headers=req_headers, timeout=timeout)
            elif method_upper == "POST":
                req_headers = dict(req_headers)
                req_headers["Content-Type"] = "application/json"
                resp = http.post(url, headers=req_headers, json=body or {}, timeout=timeout)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if resp.status_code == 401:
                raise SessionExpiredError("Session expired or invalid")
            if resp.status_code >= 400:
                raise ValueError(f"API returned {resp.status_code}: {resp.text}")
            try:
                return resp.json()
            except ValueError as e:
                raise SessionExpiredError("Session expired or invalid (non-JSON response)") from e
        except SessionExpiredError:
            _BROWSER_API_FORCE_BROWSER = True
        finally:
            http.close()

    client = _get_browser_client(session)
    try:
        return client.request_json(
            method,
            url,
            headers=headers,
            body=body,
            timeout=timeout,
        )
    except SessionExpiredError:
        _close_browser_client()
        # Auto-retry once with fresh session
        if _retry_count < 1:
            import sys

            sys.stderr.write("Session expired, re-authenticating...\n")
            sys.stderr.flush()
            clear_session_cache()
            new_session = get_web_session()
            return request_json(
                new_session,
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
                _retry_count=_retry_count + 1,
            )
        raise


def get_credentials() -> tuple[str, str]:
    return _get_credentials()


def login_with_playwright(
    username: str,
    password: str,
    base_url: str = "https://api.example.com",
    headless: bool = True,
) -> WebSession:
    return _login_with_playwright(
        username,
        password,
        base_url=base_url,
        headless=headless,
    )


def get_web_session(force_refresh: bool = False, require_workspace: bool = False) -> WebSession:
    return _get_web_session(force_refresh=force_refresh, require_workspace=require_workspace)


def fetch_node_specs(
    session: WebSession,
    compute_group_id: str,
    base_url: str = "https://api.example.com",
) -> dict:
    return _fetch_node_specs(
        session,
        compute_group_id,
        request_json_fn=request_json,
        base_url=base_url,
    )


def fetch_workspace_availability(
    session: WebSession,
    base_url: str = "https://api.example.com",
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    return _fetch_workspace_availability(
        session,
        request_json_fn=request_json,
        base_url=base_url,
        progress_callback=progress_callback,
    )


def fetch_gpu_availability(
    session: WebSession,
    compute_group_ids: list[str],
    base_url: str = "https://api.example.com",
) -> list[GPUAvailability]:
    return _fetch_gpu_availability(
        session,
        compute_group_ids,
        request_json_fn=request_json,
        base_url=base_url,
    )


def clear_session_cache() -> None:
    """Clear the cached web session."""
    if SESSION_CACHE_FILE.exists():
        SESSION_CACHE_FILE.unlink()
