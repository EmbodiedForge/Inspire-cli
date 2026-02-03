"""Legacy browser (web-session) API implementation.

This module is kept for backward compatibility with older import paths.
New code should import via `inspire.cli.utils.browser_api` (façade) or the
domain modules in `inspire.cli.utils.browser_api_*`.

This file intentionally re-exports the public browser API from
`inspire.cli.utils.browser_api` to avoid duplicating export lists.
"""

from __future__ import annotations

from inspire.cli.utils import browser_api as _browser_api
from inspire.cli.utils.browser_api_core import (  # noqa: F401
    BASE_URL,
    _browser_api_path,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _request_json,
    _run_in_thread,
)
from inspire.cli.utils.web_session import (  # noqa: F401
    DEFAULT_WORKSPACE_ID,
    WebSession,
    build_requests_session,
    get_playwright_proxy,
    get_web_session,
    request_json,
)

for _name in _browser_api.__all__:
    globals()[_name] = getattr(_browser_api, _name)

__all__ = [
    # web-session / helpers (legacy)
    "BASE_URL",
    "DEFAULT_WORKSPACE_ID",
    "WebSession",
    "build_requests_session",
    "get_playwright_proxy",
    "get_web_session",
    "request_json",
    # core helpers (legacy)
    "_browser_api_path",
    "_in_asyncio_loop",
    "_launch_browser",
    "_new_context",
    "_request_json",
    "_run_in_thread",
] + list(_browser_api.__all__)
