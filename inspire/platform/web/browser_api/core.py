"""Shared helpers for browser (web-session) APIs.

The Inspire web UI exposes additional SSO-only endpoints under a configurable prefix.
Domain modules (browser_api_*.py) use this module to avoid copy/pasting URL, prefix,
Playwright, and asyncio-thread bridging logic.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Optional, TypedDict
from urllib.parse import urlsplit

from inspire.platform import tls_policy
from inspire.platform.web.session import WebSession, get_playwright_proxy, request_json
from inspire.platform.web.session.browser import (
    PLAYWRIGHT_INSTALL_HINT as PLAYWRIGHT_INSTALL_HINT,
    is_missing_browser_error,
    missing_browser_error,
)


class _RequestRateLimitOptions(TypedDict, total=False):
    rate_limit_max_retries: int
    rate_limit_default_delay_seconds: float
    rate_limit_max_delay_seconds: float


DEFAULT_BASE_URL = "https://api.example.com"
# Backward-compatible constant (legacy imports). Prefer _get_base_url() for runtime use.
BASE_URL = DEFAULT_BASE_URL

# Default browser API prefix. The platform retired /api/v1 on 2026-10-15 (#82); /api/v2
# serves Action-style RPC (translated per-route in browser_api.v2) plus the proxy paths
# (notebook/lab, notebook/code, train_job/remote_cmd) that keep their REST shape.
DEFAULT_BROWSER_API_PREFIX = "/api/v2"


def _origin(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return f"{parts.scheme}://{parts.netloc}"


def _same_origin_or_reject(console_url: str, origin: str) -> str:
    target = str(console_url or "").strip()
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Console URL must be an absolute http(s) URL, got {console_url!r}")
    if _origin(target) != origin:
        raise ValueError(
            f"Browser commands only allow URLs on the configured console ({origin}); {target} is "
            "off-origin and would take the session cookie somewhere else"
        )
    return target


# Cached base URL and browser API prefix (loaded once at module import)
_cached_base_url: str | None = None
# Cached browser API prefix (loaded once at module import)
_cached_browser_api_prefix: str | None = None


def _get_base_url() -> str:
    """Get base URL from layered config with sane fallback."""
    global _cached_base_url

    if _cached_base_url is not None:
        return _cached_base_url

    try:
        from inspire.config import Config

        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        if config.base_url:
            _cached_base_url = config.base_url
            return _cached_base_url
    except Exception:
        pass

    _cached_base_url = os.environ.get("INSPIRE_BASE_URL", DEFAULT_BASE_URL).strip()
    return _cached_base_url


def _set_base_url(url: str) -> None:
    """Override the cached base URL for the current process.

    This is used by ``init --discover`` to propagate a CLI-provided
    ``--base-url`` into the module-level cache so that all subsequent
    browser-API calls resolve to the correct host.
    """
    global _cached_base_url, BASE_URL

    _cached_base_url = url.rstrip("/")
    BASE_URL = _cached_base_url


def _get_browser_api_prefix() -> str:
    """Get the browser API prefix from config or environment.

    Returns:
        Browser API prefix (e.g., "/api/v2" or custom)
    """
    global _cached_browser_api_prefix

    if _cached_browser_api_prefix is not None:
        return _cached_browser_api_prefix

    # Check environment variable first (highest priority)
    env_prefix = os.environ.get("INSPIRE_BROWSER_API_PREFIX", "").strip()
    if env_prefix:
        _cached_browser_api_prefix = env_prefix
        return _cached_browser_api_prefix

    # Try to load from config files
    try:
        from inspire.config import Config

        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        if config.browser_api_prefix:
            _cached_browser_api_prefix = config.browser_api_prefix
            return _cached_browser_api_prefix
    except Exception:
        pass

    # Use default
    _cached_browser_api_prefix = DEFAULT_BROWSER_API_PREFIX
    return _cached_browser_api_prefix


def _browser_api_path(endpoint_path: str) -> str:
    """Build a browser API path with configurable prefix.

    Args:
        endpoint_path: The endpoint path (e.g., "/notebook/lab/{id}/")

    Returns:
        Full path with prefix (e.g., "/api/v2/notebook/lab/{id}/"). REST-style v1
        endpoints are translated to v2 Action calls by ``browser_api.v2`` before the
        request leaves ``_request_json``; only proxy paths keep this raw shape.
    """
    endpoint = endpoint_path.lstrip("/")
    prefix = _get_browser_api_prefix().rstrip("/")
    return f"{prefix}/{endpoint}"


def _request_json(
    session: WebSession,
    method: str,
    path: str,
    *,
    referer: str,
    body: Optional[dict] = None,
    timeout: int = 30,
    rate_limit_max_retries: int | None = None,
    rate_limit_default_delay_seconds: float | None = None,
    rate_limit_max_delay_seconds: float | None = None,
) -> dict:
    # v1 REST endpoints are translated to v2 Action calls here (#82): the platform
    # retired /api/v1 on 2026-10-15, and the per-route adapters in browser_api.v2 own the
    # body/response differences. Untranslated paths (proxy routes like /notebook/lab/...,
    # explicit /api/v2 Action URLs, unmapped v1 endpoints) pass through unchanged.
    from inspire.platform.web.browser_api.v2 import resolve_v2_route, call_v2_route

    resolved = resolve_v2_route(method, path)
    if resolved is not None:
        route, path_params = resolved
        v2_rate_limit_options: dict[str, int | float] = {}
        if rate_limit_max_retries is not None:
            v2_rate_limit_options["rate_limit_max_retries"] = rate_limit_max_retries
        if rate_limit_default_delay_seconds is not None:
            v2_rate_limit_options["rate_limit_default_delay_seconds"] = (
                rate_limit_default_delay_seconds
            )
        if rate_limit_max_delay_seconds is not None:
            v2_rate_limit_options["rate_limit_max_delay_seconds"] = rate_limit_max_delay_seconds
        return call_v2_route(
            session,
            route,
            path_params,
            body=body,
            base_url=_get_base_url(),
            referer=referer,
            timeout=timeout,
            rate_limit_options=v2_rate_limit_options,
        )

    url = f"{_get_base_url()}{path}"
    headers = {"Referer": referer}
    rate_limit_options: _RequestRateLimitOptions = {}
    if rate_limit_max_retries is not None:
        rate_limit_options["rate_limit_max_retries"] = rate_limit_max_retries
    if rate_limit_default_delay_seconds is not None:
        rate_limit_options["rate_limit_default_delay_seconds"] = rate_limit_default_delay_seconds
    if rate_limit_max_delay_seconds is not None:
        rate_limit_options["rate_limit_max_delay_seconds"] = rate_limit_max_delay_seconds
    return request_json(
        session,
        method,
        url,
        headers=headers,
        body=body,
        timeout=timeout,
        **rate_limit_options,
    )


def _in_asyncio_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run_in_thread(func, *args, **kwargs):  # noqa: ANN001
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = func(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            error["exc"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error["exc"]
    return result.get("value")


def _launch_browser(p, headless: bool = True):  # noqa: ANN001
    proxy = get_playwright_proxy()
    try:
        return p.chromium.launch(
            headless=headless,
            proxy=proxy,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
    except Exception as exc:
        # A missing browser binary is an install step, not a platform failure: say
        # so and hand over the command instead of a Playwright stack trace.
        if is_missing_browser_error(exc):
            raise missing_browser_error("browser automation needs Chromium") from None
        raise


def _new_context(browser, *, storage_state=None):  # noqa: ANN001
    proxy = get_playwright_proxy()
    # Verification on by default: these contexts navigate to the CAS/Keycloak login and carry the
    # session cookie, and every host involved serves a publicly trusted chain. `ignore_https_errors`
    # is only ever True through api.skip_ssl_verify, which warns on stderr.
    ignore_https_errors = tls_policy.ignore_https_errors()
    if storage_state is not None:
        return browser.new_context(
            storage_state=storage_state,
            proxy=proxy,
            ignore_https_errors=ignore_https_errors,
        )
    return browser.new_context(proxy=proxy, ignore_https_errors=ignore_https_errors)


# Keep BASE_URL in sync for legacy imports.
BASE_URL = _get_base_url()
