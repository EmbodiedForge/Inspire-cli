"""Standalone lab URL resolution (API first, HTTP redirect, Playwright fallback)."""

from __future__ import annotations

from typing import Optional

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _launch_browser,
    _new_context,
)
from inspire.platform.web.browser_api.playwright_notebooks import (
    _is_lab_like_url,
    _wait_for_lab_handle,
)
from inspire.platform.web.browser_api.rtunnel.http_lab_resolution import (
    resolve_lab_url_http,
)
from inspire.platform.web.browser_api.rtunnel.logging import trace_event, update_trace_summary
from inspire.platform.web.session import WebSession, get_web_session

import logging

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel.lab_resolution")


def resolve_lab_url(
    notebook_id: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
    auth_method: Optional[str] = None,
) -> str:
    """Resolve the JupyterLab URL for a notebook.

    1. Queries the notebook API for ``jupyter_url`` / ``proxy_uri``.
    2. If the API returns a URL, validates it looks like a lab URL and returns it.
    3. Otherwise tries pure HTTP redirect resolution.
    4. Only falls back to Playwright if both fail and ``auth_method != "http"``.

    Returns:
        The resolved lab URL string (e.g. ``https://…/lab?token=…``).

    Raises:
        RuntimeError: if the lab URL cannot be resolved through any path.
    """
    base_url = _get_base_url()
    if session is None:
        session = get_web_session()

    # ── 1. API fast-path ────────────────────────────────────────────────────
    jupyter_url = _try_api_for_jupyter_url(notebook_id, base_url, session)
    if jupyter_url and _is_lab_like_url(
        jupyter_url, notebook_lab_pattern=_browser_api_path("/notebook/lab/")
    ):
        trace_event("lab_resolution_api", notebook_id=notebook_id, lab_url=jupyter_url)
        update_trace_summary(lab_resolution="api_direct")
        return jupyter_url

    # ── 2. HTTP redirect path ───────────────────────────────────────────────
    try:
        lab_url = resolve_lab_url_http(notebook_id, session, timeout=timeout)
        return lab_url
    except Exception:
        trace_event("lab_resolution_http_failed", notebook_id=notebook_id)

    # ── 3. Playwright fallback (only if not http-only auth) ─────────────────
    if auth_method == "http":
        raise RuntimeError(
            f"Failed to resolve JupyterLab URL for notebook {notebook_id}. "
            "HTTP paths (API and redirect) both failed and auth_method='http' "
            "disables Playwright fallback."
        )

    trace_event("lab_resolution_playwright_fallback", notebook_id=notebook_id)
    return _resolve_via_playwright(
        notebook_id, base_url, session, headless=headless, timeout=timeout
    )


def _try_api_for_jupyter_url(
    notebook_id: str,
    base_url: str,
    session: WebSession,
) -> str | None:
    """Return *jupyter_url* from the notebook detail API, or None."""
    try:
        from inspire.platform.web.browser_api.core import _request_json as browser_request

        data = browser_request(
            session,
            "GET",
            f"/api/v1/notebook/{notebook_id}",
            referer=f"{base_url}/jobs/interactiveModeling",
            timeout=10,
        )
        notebook_data = data.get("data", {})
        jupyter_url = notebook_data.get("jupyter_url") or notebook_data.get("proxy_uri")
        if jupyter_url:
            trace_event(
                "lab_api_jupyter_url_found", notebook_id=notebook_id, jupyter_url=jupyter_url
            )
            return jupyter_url
    except Exception as exc:
        trace_event("lab_api_jupyter_url_failed", notebook_id=notebook_id, error=str(exc))
    return None


def _resolve_via_playwright(
    notebook_id: str,
    base_url: str,
    session: WebSession,
    *,
    headless: bool = True,
    timeout: int = 60,
) -> str:
    """Navigate with Playwright and extract the lab frame URL."""
    from playwright.sync_api import sync_playwright

    timeout_ms = max(int(timeout * 1000), 10000)
    timeout_s = max(timeout_ms // 1000, 10)
    notebook_lab_pattern = _browser_api_path("/notebook/lab/")

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        try:
            context = _new_context(browser, storage_state=session.storage_state)
            page = context.new_page()
        except Exception:
            browser.close()
            raise

        try:
            # Attempt IDE navigation first
            page.goto(
                f"{base_url}/ide?notebook_id={notebook_id}",
                timeout=timeout_ms,
                wait_until="domcontentloaded",
            )

            frame_probe_s = min(10.0, max(4.0, timeout_s / 6.0))
            lab_handle = _wait_for_lab_handle(
                page,
                notebook_lab_pattern=notebook_lab_pattern,
                timeout_s=frame_probe_s,
            )
            if lab_handle is not None:
                lab_url = getattr(lab_handle, "url", "")
                if lab_url:
                    trace_event(
                        "lab_resolution_ide_frame", notebook_id=notebook_id, lab_url=lab_url
                    )
                    update_trace_summary(lab_resolution="ide_frame_probe")
                    return lab_url

            # Direct lab fallback
            notebook_lab_prefix = _browser_api_path("/notebook/lab").rstrip("/")
            direct_lab_url = f"{base_url}{notebook_lab_prefix}/{notebook_id}/"
            trace_event(
                "lab_resolution_direct_fallback", notebook_id=notebook_id, url=direct_lab_url
            )
            elapsed_ms = int(frame_probe_s * 1000)
            remaining_ms = max(10000, timeout_ms - elapsed_ms)
            direct_timeout_ms = min(remaining_ms, 20000)
            page.goto(
                direct_lab_url,
                timeout=direct_timeout_ms,
                wait_until="domcontentloaded",
            )
            lab_handle = _wait_for_lab_handle(
                page,
                notebook_lab_pattern=notebook_lab_pattern,
                timeout_s=min(5.0, max(1.0, remaining_ms / 1000.0)),
            )
            if lab_handle is not None:
                lab_url = getattr(lab_handle, "url", "")
                if lab_url:
                    trace_event("lab_resolution_direct", notebook_id=notebook_id, lab_url=lab_url)
                    update_trace_summary(lab_resolution="direct_lab_fallback")
                    return lab_url

            frame_urls = [fr.url for fr in getattr(page, "frames", []) if getattr(fr, "url", "")]
            trace_event(
                "lab_resolution_failed",
                notebook_id=notebook_id,
                final_page_url=getattr(page, "url", "") or "(empty)",
                frame_count=len(frame_urls),
            )
            update_trace_summary(
                lab_resolution="failed",
                last_error="Failed to resolve notebook JupyterLab to a usable frame or page.",
            )
            raise RuntimeError(
                "Failed to resolve notebook JupyterLab to a usable frame or page.\n"
                f"Final page URL: {getattr(page, 'url', '') or '(empty)'}\n"
                f"Frame URLs: {frame_urls or ['(none)']}"
            )
        finally:
            try:
                context.close()
            finally:
                browser.close()
