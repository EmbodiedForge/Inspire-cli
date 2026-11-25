"""Execute caller-provided JavaScript in an authenticated console page."""

from __future__ import annotations

from typing import Any, Optional

from inspire.platform.web.session import (
    SessionExpiredError,
    WebSession,
    _refresh_session_in_place,
    get_web_session,
)

from .core import (
    PLAYWRIGHT_INSTALL_HINT,
    _get_base_url,
    _launch_browser,
    _new_context,
    _origin,
    _same_origin_or_reject,
)

_TIMEOUT_MARKER = "inspire api eval asynchronous execution timed out"
_EVALUATE_SCRIPT = """
async ({source, timeoutMs, marker}) => {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${marker} after ${timeoutMs} ms`)), timeoutMs);
  });
  try {
    const evaluated = (0, eval)(source);
    const pending = typeof evaluated === "function" ? evaluated() : evaluated;
    return await Promise.race([Promise.resolve(pending), deadline]);
  } finally {
    clearTimeout(timer);
  }
}
"""


class PageEvaluationTimeout(TimeoutError):
    """The navigation or asynchronous page evaluation exceeded its watching budget."""


class InvalidConsoleUrl(ValueError):
    """The initial console URL failed local origin validation."""


class _AuthenticationRedirect(RuntimeError):
    """The initial page navigation left the console origin before any caller JS ran."""


def evaluate_page(
    source: str,
    *,
    session: Optional[WebSession] = None,
    console_url: Optional[str] = None,
    timeout_seconds: int = 30,
) -> Any:
    """Evaluate one JavaScript program in a fresh, authenticated console page."""
    expression = str(source or "").strip()
    if not expression:
        raise ValueError("JavaScript source must not be empty")
    wait_seconds = int(timeout_seconds)
    if wait_seconds < 1:
        raise ValueError("Browser evaluation timeout must be positive")

    base_url = _get_base_url().rstrip("/")
    origin = _origin(f"{base_url}/")
    try:
        target = _same_origin_or_reject(console_url or f"{base_url}/", origin)
    except ValueError as exc:
        raise InvalidConsoleUrl(str(exc)) from exc
    current_session = session if session is not None else get_web_session()

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(PLAYWRIGHT_INSTALL_HINT) from None

    def run_once(web_session: WebSession) -> Any:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, headless=True)
            try:
                context = _new_context(browser, storage_state=web_session.storage_state)
                page = context.new_page()
                try:
                    page.goto(
                        target,
                        wait_until="domcontentloaded",
                        timeout=min(wait_seconds * 1000, 30_000),
                    )
                    if _origin(page.url) != origin:
                        raise _AuthenticationRedirect
                    return page.evaluate(
                        _EVALUATE_SCRIPT,
                        {
                            "source": expression,
                            "timeoutMs": wait_seconds * 1000,
                            "marker": _TIMEOUT_MARKER,
                        },
                    )
                except PlaywrightTimeoutError as exc:
                    raise PageEvaluationTimeout(
                        f"Browser page evaluation exceeded {wait_seconds} seconds"
                    ) from exc
                except Exception as exc:
                    if _TIMEOUT_MARKER in str(exc):
                        raise PageEvaluationTimeout(
                            f"Browser page evaluation exceeded {wait_seconds} seconds"
                        ) from exc
                    raise
            finally:
                browser.close()

    for attempt in range(2):
        try:
            return run_once(current_session)
        except _AuthenticationRedirect:
            if attempt == 1:
                raise SessionExpiredError(
                    "Console authentication redirected away from the configured origin after "
                    "one session refresh; run `inspire init` to re-authenticate"
                ) from None
            refreshed_session = get_web_session(
                force_refresh=True,
                account=current_session.account_alias,
            )
            _refresh_session_in_place(current_session, refreshed_session)
    raise AssertionError("unreachable browser evaluation retry state")


__all__ = ["InvalidConsoleUrl", "PageEvaluationTimeout", "evaluate_page"]
