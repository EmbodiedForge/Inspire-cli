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


def open_notebook_lab(page, *, notebook_id: str):  # noqa: ANN001
    """Open the notebook's JupyterLab and return the lab frame/page handle."""
    base_url = _get_base_url()
    page.goto(
        f"{base_url}/ide?notebook_id={notebook_id}",
        timeout=60000,
        wait_until="domcontentloaded",
    )

    start = time.time()
    lab_frame = None
    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    while time.time() - start < 60:
        for fr in page.frames:
            url = fr.url or ""
            if "notebook-inspire" in url and url.rstrip("/").endswith("/lab"):
                lab_frame = fr
                break
            if notebook_lab_pattern.lstrip("/") in url:
                lab_frame = fr
                break
        if lab_frame:
            break
        page.wait_for_timeout(500)

    if lab_frame is None:
        notebook_lab_prefix = _browser_api_path("/notebook/lab").rstrip("/")
        direct_lab_url = f"{base_url}{notebook_lab_prefix}/{notebook_id}/"
        page.goto(
            direct_lab_url,
            timeout=60000,
            wait_until="domcontentloaded",
        )
        lab_frame = page

    return lab_frame


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
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=180000)
            except Exception:
                pass

            terminal_opened = False

            terminal_card = lab_frame.locator(
                "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
            )
            try:
                terminal_card.first.wait_for(state="visible", timeout=20000)
                terminal_card.first.click(timeout=8000)
                terminal_opened = True
            except Exception:
                terminal_opened = False

            if not terminal_opened:
                try:
                    launcher_btn = lab_frame.locator(
                        "button[title*='Launcher'], button[aria-label*='Launcher']"
                    ).first
                    if launcher_btn.count() > 0:
                        launcher_btn.click(timeout=2000)
                        page.wait_for_timeout(500)
                    terminal_card = lab_frame.locator(
                        "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                    )
                    terminal_card.first.wait_for(state="visible", timeout=20000)
                    terminal_card.first.click(timeout=8000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            if not terminal_opened:
                raise ValueError("Failed to open Jupyter terminal")

            try:
                term_focus = lab_frame.locator(
                    "textarea.xterm-helper-textarea, textarea.xterm-helper-textarea, "
                    "div.xterm-helper-textarea textarea"
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
