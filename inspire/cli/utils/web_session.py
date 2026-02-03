"""Web session management for web UI APIs.

This module keeps the historical import path stable while splitting the implementation into
smaller, testable modules:
- `web_session_models`: models + cache persistence
- `web_session_proxy`: proxy discovery for Playwright
- `web_session_auth`: login + session refresh
- `web_session_workspace`: workspace availability helpers
"""

from __future__ import annotations

import atexit
import hashlib
import json
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests

from inspire.cli.utils.web_session_auth import (
    get_credentials as _get_credentials,
    get_web_session as _get_web_session,
    login_with_playwright as _login_with_playwright,
)
from inspire.cli.utils.web_session_models import (
    DEFAULT_WORKSPACE_ID,
    SESSION_CACHE_FILE,
    SESSION_TTL,
    SessionExpiredError,
    WebSession,
)
from inspire.cli.utils.web_session_proxy import get_playwright_proxy
from inspire.cli.utils.web_session_workspace import (
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


def _cookie_jar_from_session(
    session: "WebSession", base_url: str
) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    base_host = urlsplit(base_url).hostname or ""

    storage_cookies = session.storage_state.get("cookies") if session.storage_state else None
    if storage_cookies:
        for cookie in storage_cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name:
                continue
            domain = cookie.get("domain") or base_host
            path = cookie.get("path") or "/"
            jar.set(name, value, domain=domain, path=path)

    if not storage_cookies and session.cookies:
        for name, value in session.cookies.items():
            if not name:
                continue
            jar.set(name, value, domain=base_host, path="/")

    return jar


def build_requests_session(session: "WebSession", base_url: str) -> requests.Session:
    storage_cookies = session.storage_state.get("cookies") if session.storage_state else None
    if not storage_cookies and not session.cookies:
        raise ValueError("Session expired or invalid (missing storage state)")

    http = requests.Session()
    http.cookies.update(_cookie_jar_from_session(session, base_url))
    http.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
    )
    return http


class _BrowserRequestClient:
    def __init__(self, session: "WebSession") -> None:
        from playwright.sync_api import sync_playwright

        proxy = get_playwright_proxy()
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True, proxy=proxy)
        self._context = self._browser.new_context(
            storage_state=session.storage_state,
            proxy=proxy,
            ignore_https_errors=True,
        )
        self.session_fingerprint = _session_fingerprint(session)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[dict] = None,
        timeout: int = 30,
    ) -> dict:
        req_headers = headers or {}
        method_upper = method.upper()
        timeout_ms = timeout * 1000

        if method_upper == "GET":
            resp = self._context.request.get(url, headers=req_headers, timeout=timeout_ms)
        elif method_upper == "POST":
            post_headers = dict(req_headers)
            if not any(key.lower() == "content-type" for key in post_headers):
                post_headers["Content-Type"] = "application/json"
            resp = self._context.request.post(
                url,
                headers=post_headers,
                data=json.dumps(body or {}),
                timeout=timeout_ms,
            )
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if resp.status == 401:
            raise SessionExpiredError("Session expired or invalid")
        if resp.status >= 400:
            raise ValueError(f"API returned {resp.status}")

        return resp.json()

    def close(self) -> None:
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass


_BROWSER_API_FORCE_BROWSER = False
_BROWSER_CLIENT: Optional[_BrowserRequestClient] = None


def _session_fingerprint(session: "WebSession") -> str:
    cookies = session.storage_state.get("cookies") if session.storage_state else []
    payload = json.dumps(
        [
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path"),
            }
            for c in cookies or []
        ],
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _get_browser_client(session: "WebSession") -> _BrowserRequestClient:
    global _BROWSER_CLIENT

    fingerprint = _session_fingerprint(session)
    if _BROWSER_CLIENT and _BROWSER_CLIENT.session_fingerprint == fingerprint:
        return _BROWSER_CLIENT

    if _BROWSER_CLIENT:
        _BROWSER_CLIENT.close()

    _BROWSER_CLIENT = _BrowserRequestClient(session)
    return _BROWSER_CLIENT


def _close_browser_client() -> None:
    global _BROWSER_CLIENT
    if _BROWSER_CLIENT:
        _BROWSER_CLIENT.close()
        _BROWSER_CLIENT = None


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
