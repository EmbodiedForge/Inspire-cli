"""Browser (web-session) notebook APIs (Playwright terminal command runner)."""

from __future__ import annotations

from typing import Optional

from inspire.cli.utils.browser_api_core import (
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.cli.utils.web_session import WebSession, get_web_session

from .jupyter import open_notebook_lab


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
