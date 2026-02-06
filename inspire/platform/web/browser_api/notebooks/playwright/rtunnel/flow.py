"""Implementation for notebook rtunnel setup via Playwright."""

from __future__ import annotations

from typing import Optional

from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.core import (
    _launch_browser,
    _new_context,
)
from inspire.platform.web.session import WebSession, get_web_session

from ..jupyter import build_jupyter_proxy_url, open_notebook_lab
from .commands import build_rtunnel_setup_commands
from .probe import probe_existing_rtunnel_proxy_url
from .verify import wait_for_rtunnel_reachable


def _setup_notebook_rtunnel_sync(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Sync implementation for setup_notebook_rtunnel."""
    import sys as _sys

    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    existing = probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=port,
        session=session,
    )
    if existing:
        _sys.stderr.write("Using existing rtunnel connection (fast path).\n")
        _sys.stderr.flush()
        return existing

    _sys.stderr.write("Setting up rtunnel tunnel via browser automation...\n")
    _sys.stderr.flush()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()

        try:
            lab_frame = open_notebook_lab(page, notebook_id=notebook_id)
            jupyter_proxy_url = build_jupyter_proxy_url(lab_frame.url, port=port)

            try:
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=180000)
            except Exception:
                pass

            try:
                lab_frame.locator(
                    "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                ).first.wait_for(
                    state="visible",
                    timeout=180000,
                )
            except Exception:
                try:
                    lab_frame.get_by_role("menuitem", name="File").first.wait_for(
                        state="visible",
                        timeout=180000,
                    )
                except Exception:
                    lab_frame.get_by_role("menuitem", name="文件").first.wait_for(
                        state="visible",
                        timeout=180000,
                    )

            for label in ("No", "Yes", "否", "不接收", "取消"):
                try:
                    btn = lab_frame.get_by_role("button", name=label)
                    if btn.count() > 0:
                        btn.first.click(timeout=1000)
                        break
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
                try:
                    try:
                        lab_frame.get_by_role("menuitem", name="File").first.click(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="New").first.hover(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="Terminal").first.click(timeout=5000)
                    except Exception:
                        lab_frame.get_by_role("menuitem", name="文件").first.click(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="新建").first.hover(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="终端").first.click(timeout=5000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            if not terminal_opened:
                raise ValueError("Failed to open Jupyter terminal")

            try:
                term_tab = lab_frame.locator(
                    "li.lm-TabBar-tab:has-text('Terminal'), li.lm-TabBar-tab:has-text('终端')"
                ).first
                if term_tab.count() > 0:
                    term_tab.click(timeout=2000)
                    page.wait_for_timeout(250)
            except Exception:
                pass

            try:
                term_focus = lab_frame.locator(
                    "textarea.xterm-helper-textarea, textarea.xterm-helper-textarea, "
                    "div.xterm-helper-textarea textarea"
                ).first
                if term_focus.count() > 0:
                    term_focus.click(timeout=2000)
            except Exception:
                pass

            cmd_lines = build_rtunnel_setup_commands(
                port=port,
                ssh_port=ssh_port,
                ssh_public_key=ssh_public_key,
                ssh_runtime=ssh_runtime,
            )

            _sys.stderr.write("  Executing setup commands in notebook terminal...\n")
            _sys.stderr.flush()
            for line in cmd_lines:
                page.keyboard.type(line, delay=2)
                page.keyboard.press("Enter")
                page.wait_for_timeout(200)

            _sys.stderr.write("  Waiting for services to start...\n")
            _sys.stderr.flush()
            page.wait_for_timeout(5000)
            try:
                page.screenshot(path="/tmp/notebook_terminal_debug.png")
            except Exception:
                pass

            proxy_url = None
            try:
                vscode_tab = page.locator('img[alt="vscode"]').first
                if vscode_tab.count() > 0:
                    vscode_tab.click(timeout=5000)
                    page.wait_for_timeout(3000)

                vscode_url = None
                for fr in page.frames:
                    if "/vscode/" in fr.url:
                        vscode_url = fr.url
                        break

                if vscode_url:
                    from urllib.parse import parse_qs, urlparse

                    parsed = urlparse(vscode_url)
                    token = parse_qs(parsed.query).get("token", [None])[0]
                    base = vscode_url.split("?", 1)[0].rstrip("/")
                    proxy_url = f"{base}/proxy/{port}/"
                    if token:
                        proxy_url = f"{proxy_url}?token={token}"
            except Exception:
                proxy_url = None

            if not proxy_url:
                proxy_url = jupyter_proxy_url

            _sys.stderr.write("  Verifying rtunnel is reachable...\n")
            _sys.stderr.flush()
            wait_for_rtunnel_reachable(
                proxy_url=proxy_url,
                timeout_s=timeout,
                context=context,
                page=page,
            )
            return proxy_url

        finally:
            try:
                context.close()
            finally:
                browser.close()
