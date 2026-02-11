"""Playwright-based notebook automation (exec + Jupyter navigation)."""

from __future__ import annotations

import time
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.platform.web.session import WebSession, get_web_session


# ---------------------------------------------------------------------------
# Jupyter navigation
# ---------------------------------------------------------------------------


def _is_lab_like_url(url: str, *, notebook_lab_pattern: str) -> bool:
    value = str(url or "")
    if not value:
        return False

    normalized = value.rstrip("/")
    if "notebook-inspire" in value and normalized.endswith("/lab"):
        return True
    if notebook_lab_pattern.lstrip("/") in value:
        return True
    if "/jupyter/" in value and normalized.endswith("/lab"):
        return True
    return False


def _find_lab_handle(page, *, notebook_lab_pattern: str):  # noqa: ANN001
    for fr in page.frames:
        if _is_lab_like_url(fr.url or "", notebook_lab_pattern=notebook_lab_pattern):
            return fr

    page_url = getattr(page, "url", "") or ""
    if _is_lab_like_url(page_url, notebook_lab_pattern=notebook_lab_pattern):
        return page

    return None


def _wait_for_lab_handle(
    page,  # noqa: ANN001
    *,
    notebook_lab_pattern: str,
    timeout_s: float,
):
    start = time.time()
    while time.time() - start < timeout_s:
        handle = _find_lab_handle(page, notebook_lab_pattern=notebook_lab_pattern)
        if handle is not None:
            return handle
        page.wait_for_timeout(500)
    return None


def open_notebook_lab(page, *, notebook_id: str, timeout: int = 60000):  # noqa: ANN001
    """Open the notebook's JupyterLab and return the lab frame/page handle."""
    base_url = _get_base_url()
    timeout_ms = max(int(timeout), 10000)
    timeout_s = max(timeout_ms // 1000, 10)
    page.goto(
        f"{base_url}/ide?notebook_id={notebook_id}",
        timeout=timeout_ms,
        wait_until="domcontentloaded",
    )

    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    frame_probe_s = min(10.0, max(4.0, timeout_s / 6.0))
    lab_handle = _wait_for_lab_handle(
        page,
        notebook_lab_pattern=notebook_lab_pattern,
        timeout_s=frame_probe_s,
    )
    if lab_handle is not None:
        return lab_handle

    notebook_lab_prefix = _browser_api_path("/notebook/lab").rstrip("/")
    direct_lab_url = f"{base_url}{notebook_lab_prefix}/{notebook_id}/"
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
        return lab_handle

    return page


def build_jupyter_proxy_url(lab_url: str, *, port: int) -> str:
    """Build a Jupyter proxy URL for the given lab URL and port."""
    parsed = urlsplit(lab_url)
    query_token = parse_qs(parsed.query).get("token", [None])[0]

    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    if notebook_lab_pattern.lstrip("/") in lab_url:
        base_path = parsed.path
        if not base_path.endswith("/"):
            base_path = base_path + "/"
        base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
        proxy_url = f"{base_url}proxy/{port}/"
        if query_token:
            return f"{proxy_url}?{urlencode({'token': query_token})}"
        return proxy_url

    path_parts = [part for part in parsed.path.split("/") if part]
    path_token = None
    try:
        jupyter_index = path_parts.index("jupyter")
        if len(path_parts) > jupyter_index + 2:
            path_token = path_parts[jupyter_index + 2]
    except ValueError:
        path_token = None

    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/lab"):
        base_path = base_path[:-4]
    proxy_path = f"{base_path}/proxy/{port}/"

    token = query_token or path_token
    query = urlencode({"token": token}) if token else ""
    return urlunsplit((parsed.scheme, parsed.netloc, proxy_path, query, ""))


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def run_command_in_notebook(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
) -> None:
    """Run a command in a notebook's Jupyter terminal."""
    if _in_asyncio_loop():
        return _run_in_thread(
            _run_command_in_notebook_sync,
            notebook_id=notebook_id,
            command=command,
            session=session,
            headless=headless,
            timeout=timeout,
        )
    return _run_command_in_notebook_sync(
        notebook_id=notebook_id,
        command=command,
        session=session,
        headless=headless,
        timeout=timeout,
    )


def _run_command_in_notebook_sync(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
) -> None:
    """Sync implementation for run_command_in_notebook."""
    import sys as _sys

    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    _sys.stderr.write("Running command in notebook terminal...\n")
    _sys.stderr.flush()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()

        try:
            lab_frame = open_notebook_lab(page, notebook_id=notebook_id)

            try:
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=30000)
            except Exception:
                pass

            terminal_opened = False

            # Strategy 1: REST API
            try:
                from inspire.platform.web.browser_api.rtunnel import (
                    _create_terminal_via_api,
                    _jupyter_server_base,
                )

                term_name = _create_terminal_via_api(context, lab_frame.url)
                if term_name:
                    server_base = _jupyter_server_base(lab_frame.url)
                    term_url = f"{server_base}lab/terminals/{term_name}?reset"
                    lab_frame.goto(term_url, timeout=15000, wait_until="domcontentloaded")
                    lab_frame.locator(".xterm").first.wait_for(state="attached", timeout=10000)
                    terminal_opened = True
            except Exception:
                pass

            # Strategy 2: launcher card
            if not terminal_opened:
                terminal_card = lab_frame.locator(
                    "div.jp-LauncherCard:has-text('Terminal'), "
                    "div.jp-LauncherCard:has-text('终端')"
                )
                try:
                    terminal_card.first.wait_for(state="visible", timeout=20000)
                    terminal_card.first.click(timeout=8000)
                    terminal_opened = True
                except Exception:
                    pass

            # Strategy 3: launcher button → launcher card
            if not terminal_opened:
                try:
                    launcher_btn = lab_frame.locator(
                        "button[title*='Launcher'], button[aria-label*='Launcher']"
                    ).first
                    if launcher_btn.count() > 0:
                        launcher_btn.click(timeout=2000)
                        page.wait_for_timeout(500)
                    terminal_card = lab_frame.locator(
                        "div.jp-LauncherCard:has-text('Terminal'), "
                        "div.jp-LauncherCard:has-text('终端')"
                    )
                    terminal_card.first.wait_for(state="visible", timeout=20000)
                    terminal_card.first.click(timeout=8000)
                    terminal_opened = True
                except Exception:
                    pass

            if not terminal_opened:
                raise ValueError("Failed to open Jupyter terminal")

            try:
                term_focus = lab_frame.locator(
                    "textarea.xterm-helper-textarea, " "div.xterm-helper-textarea textarea"
                ).first
                if term_focus.count() > 0:
                    term_focus.click(timeout=2000)
            except Exception:
                pass

            page.keyboard.type(command, delay=2)
            page.keyboard.press("Enter")

            page.wait_for_timeout(int(timeout * 1000))

        finally:
            try:
                context.close()
            finally:
                browser.close()


__all__ = [
    "build_jupyter_proxy_url",
    "open_notebook_lab",
    "run_command_in_notebook",
]
