"""Playwright-based request client used as a fallback when cookies expire."""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from inspire.cli.utils.web_session_models import SessionExpiredError, WebSession
from inspire.cli.utils.web_session_proxy import get_playwright_proxy


class _BrowserRequestClient:
    def __init__(self, session: WebSession) -> None:
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


def _session_fingerprint(session: WebSession) -> str:
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


_BROWSER_CLIENT: Optional[_BrowserRequestClient] = None


def _get_browser_client(session: WebSession) -> _BrowserRequestClient:
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
