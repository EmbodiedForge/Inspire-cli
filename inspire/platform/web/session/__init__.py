"""Web session management for web UI APIs."""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional  # noqa: F401
from urllib.parse import urlsplit

from inspire.platform.web.session.auth import (
    get_credentials as _get_credentials,
    get_web_session as _get_web_session,
    login_with_playwright as _login_with_playwright,
    login_with_requests as _login_with_requests,
)
from inspire.platform.web.session.http_auth import refresh_with_requests
from inspire.platform.web.session.models import (
    DEFAULT_WORKSPACE_ID,
    SESSION_CACHE_FILE,
    SESSION_TTL,
    SessionAuthenticationError,
    SessionExpiredError,
    SessionRefreshError,
    WebSession,
    _lock_web_session_cache,
    get_auth_failure_cache_file,
    get_session_cache_file,
)
from inspire.platform.web.session.proxy import get_playwright_proxy
from inspire.platform.web.session.requests import build_requests_session
from inspire.platform.web.session.workspace import (
    fetch_node_dimensions as _fetch_node_dimensions,
)

logger = logging.getLogger(__name__)

_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_DEFAULT_DELAY_SECONDS = 0.5
_RATE_LIMIT_MAX_DELAY_SECONDS = 5.0
_RATE_LIMIT_RESET_CUSHION_SECONDS = 0.05


class RateLimitError(ValueError):
    """Raised after a bounded HTTP 429 retry policy is exhausted."""

    def __init__(self, *, path: str, attempts: int, detail: str) -> None:
        self.path = path
        self.attempts = attempts
        self.detail = detail
        super().__init__(f"API returned 429 for {path} after {attempts} attempts: {detail}")


__all__ = [
    "DEFAULT_WORKSPACE_ID",
    "SESSION_CACHE_FILE",
    "SESSION_TTL",
    "SessionAuthenticationError",
    "SessionExpiredError",
    "RateLimitError",
    "SessionRefreshError",
    "WebSession",
    "build_requests_session",
    "clear_session_cache",
    "clear_account_session_cache",
    "fetch_node_dimensions",
    "get_credentials",
    "get_playwright_proxy",
    "get_web_session",
    "login_with_playwright",
    "login_with_requests",
    "request_json",
]


def _refresh_session_in_place(current: "WebSession", refreshed: "WebSession") -> None:
    """Replace an existing session object's fields with refreshed credentials/state."""
    current.storage_state = refreshed.storage_state
    current.cookies = refreshed.cookies
    current.workspace_id = refreshed.workspace_id
    current.account_alias = refreshed.account_alias
    current.login_username = refreshed.login_username
    current.base_url = refreshed.base_url
    current.all_workspace_ids = refreshed.all_workspace_ids
    current.all_workspace_names = refreshed.all_workspace_names
    current.created_at = refreshed.created_at
    # A successful refresh completes the current retry cycle.  Keep failures
    # latched, but allow a later, independent request in the same long-running
    # process to refresh again when those credentials eventually expire.
    current._refresh_attempted = False
    current._refresh_failure_message = None


def _rate_limit_retry_delay(
    response: object,
    attempt: int,
    *,
    default_delay_seconds: float,
    max_delay_seconds: float,
) -> float:
    """Return a bounded retry delay from standard/APISIX rate-limit headers."""
    exponential_delay = default_delay_seconds * (2**attempt)
    headers = getattr(response, "headers", {}) or {}
    for name in ("Retry-After", "X-RateLimit-Reset"):
        raw_value = str(headers.get(name) or "").strip()
        if not raw_value:
            continue
        try:
            delay = float(raw_value)
        except ValueError:
            continue
        # Some gateways report a Unix timestamp; APISIX currently reports
        # relative seconds (for example ``X-RateLimit-Reset: 1``).
        if delay > 1_000_000:
            delay -= time.time()
        header_delay = max(delay, 0.0) + _RATE_LIMIT_RESET_CUSHION_SECONDS
        return min(max(header_delay, exponential_delay), max_delay_seconds)
    return min(exponential_delay, max_delay_seconds)


def request_json(
    session: "WebSession",
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict | list] = None,
    timeout: int = 30,
    rate_limit_max_retries: int = _RATE_LIMIT_MAX_RETRIES,
    rate_limit_default_delay_seconds: float = _RATE_LIMIT_DEFAULT_DELAY_SECONDS,
    rate_limit_max_delay_seconds: float = _RATE_LIMIT_MAX_DELAY_SECONDS,
    _retry_count: int = 0,
) -> dict:
    """Make an HTTP request and return JSON response.

    On 401 or non-JSON response, auto-retry once with a fresh session. HTTP
    429 responses use a bounded retry loop that honors gateway reset headers.
    """
    if session._refresh_failure_message:
        raise SessionRefreshError(session._refresh_failure_message)

    http = build_requests_session(session, url)
    try:
        method_upper = method.upper()
        req_headers = headers or {}
        json_body = body if body is not None else {}
        rate_limit_attempt = 0
        while True:
            if method_upper == "GET":
                if body is None:
                    resp = http.get(url, headers=req_headers, timeout=timeout)
                else:
                    req_headers = dict(req_headers)
                    req_headers["Content-Type"] = "application/json"
                    resp = http.get(url, headers=req_headers, json=body, timeout=timeout)
            elif method_upper == "POST":
                req_headers = dict(req_headers)
                req_headers["Content-Type"] = "application/json"
                resp = http.post(url, headers=req_headers, json=json_body, timeout=timeout)
            elif method_upper == "DELETE":
                req_headers = dict(req_headers)
                req_headers["Content-Type"] = "application/json"
                resp = http.delete(url, headers=req_headers, json=json_body, timeout=timeout)
            elif method_upper == "PUT":
                req_headers = dict(req_headers)
                req_headers["Content-Type"] = "application/json"
                resp = http.put(url, headers=req_headers, json=json_body, timeout=timeout)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if resp.status_code != 429 or rate_limit_attempt >= rate_limit_max_retries:
                break
            delay = _rate_limit_retry_delay(
                resp,
                rate_limit_attempt,
                default_delay_seconds=rate_limit_default_delay_seconds,
                max_delay_seconds=rate_limit_max_delay_seconds,
            )
            logger.info(
                "API rate limit reached for %s; retrying in %.2fs (%d/%d)",
                urlsplit(url).path,
                delay,
                rate_limit_attempt + 1,
                rate_limit_max_retries,
            )
            time.sleep(delay)
            rate_limit_attempt += 1

        if resp.status_code == 401:
            raise SessionExpiredError("Session expired or invalid")
        if resp.status_code == 429:
            raise RateLimitError(
                path=urlsplit(url).path,
                attempts=rate_limit_attempt + 1,
                detail=resp.text,
            )
        if resp.status_code >= 400:
            message = f"API returned {resp.status_code}: {resp.text}"
            if resp.status_code == 404 and not session.cookies:
                # The gateway answers 404 for unauthenticated calls exactly as it does
                # for a route that never existed, so a missing session reads as a
                # routing bug. Say which one it is (#84).
                message += (
                    " (no web session is loaded, and this gateway also answers 404 for "
                    "unauthenticated calls; run `inspire init` or import a browser "
                    "session first)"
                )
            raise ValueError(message)
        try:
            return resp.json()
        except ValueError as e:
            raise SessionExpiredError("Session expired or invalid (non-JSON response)") from e
    except SessionExpiredError:
        if _retry_count < 1 and not session._refresh_attempted:
            session._refresh_attempted = True
            base_url = str(session.base_url or "").strip().rstrip("/")
            if not base_url:
                parts = urlsplit(url)
                base_url = f"{parts.scheme}://{parts.netloc}"
            logger.warning("Session expired, refreshing SSO cookies...")
            try:
                refreshed = refresh_with_requests(session, base_url)
            except Exception as exc:
                logger.info("SSO cookie refresh failed: %s", exc)
            else:
                _refresh_session_in_place(session, refreshed)
                try:
                    session.save(account=session.account_alias)
                except Exception:
                    logger.debug("Could not persist the refreshed web session", exc_info=True)
                return request_json(
                    session,
                    method,
                    url,
                    headers=headers,
                    body=body,
                    timeout=timeout,
                    rate_limit_max_retries=rate_limit_max_retries,
                    rate_limit_default_delay_seconds=rate_limit_default_delay_seconds,
                    rate_limit_max_delay_seconds=rate_limit_max_delay_seconds,
                    _retry_count=_retry_count + 1,
                )
            logger.warning("Session expired, re-authenticating...")
            try:
                new_session = get_web_session(
                    force_refresh=True,
                    account=session.account_alias,
                )
            except Exception as exc:
                message = f"Session re-authentication failed: {exc}"
                session._refresh_failure_message = message
                raise SessionRefreshError(message) from exc
            _refresh_session_in_place(session, new_session)
            return request_json(
                session,
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
                rate_limit_max_retries=rate_limit_max_retries,
                rate_limit_default_delay_seconds=rate_limit_default_delay_seconds,
                rate_limit_max_delay_seconds=rate_limit_max_delay_seconds,
                _retry_count=_retry_count + 1,
            )
        message = "Session remained invalid after re-authentication"
        session._refresh_failure_message = message
        # The swallowed SessionExpiredError is the condition this branch tried to
        # recover from, so chaining it would only repeat the trigger.
        raise SessionRefreshError(message) from None
    finally:
        http.close()


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


def login_with_requests(
    username: str,
    password: str,
    base_url: str = "https://api.example.com",
    headless: bool = True,
) -> WebSession:
    return _login_with_requests(
        username,
        password,
        base_url=base_url,
        headless=headless,
    )


def get_web_session(
    force_refresh: bool = False,
    require_workspace: bool = False,
    account: Optional[str] = None,
) -> WebSession:
    return _get_web_session(
        force_refresh=force_refresh, require_workspace=require_workspace, account=account
    )


def fetch_node_dimensions(
    session: WebSession,
    *,
    base_url: str = "https://api.example.com",
    workspace_id: str,
    logic_compute_group_id: str,
    page_size: int = 1000,
) -> dict:
    return _fetch_node_dimensions(
        session,
        request_json_fn=request_json,
        base_url=base_url,
        workspace_id=workspace_id,
        logic_compute_group_id=logic_compute_group_id,
        page_size=page_size,
    )


def clear_session_cache() -> None:
    """Clear the cached web session.

    The data-square portal caches its own cookie next to ours (see
    ``inspire.platform.web.datasquare.session``); it is cleared here too so one
    `logout`/`config clear` cannot leave a live second session behind.
    """
    cache_dir = SESSION_CACHE_FILE.parent
    if not cache_dir.exists():
        return
    for pattern in ("web_session*.json", "datasquare_session*.json"):
        for cache_file in cache_dir.glob(pattern):
            try:
                if pattern == "web_session*.json":
                    with _lock_web_session_cache(cache_file):
                        cache_file.unlink()
                else:
                    cache_file.unlink()
            except Exception:
                continue
    for cache_file in cache_dir.glob("web_auth_failure*.json"):
        try:
            cache_file.unlink()
        except Exception:
            continue


def clear_account_session_cache(account: str) -> None:
    """Clear only the web, auth-failure, and data-square caches for one account."""
    path = get_session_cache_file(account)
    with _lock_web_session_cache(path):
        path.unlink(missing_ok=True)
    try:
        get_auth_failure_cache_file(account).unlink()
    except FileNotFoundError:
        pass

    from inspire.platform.web.datasquare.session import get_datasquare_session_cache_file

    try:
        get_datasquare_session_cache_file(account).unlink()
    except FileNotFoundError:
        pass
