"""Implementation for notebook rtunnel setup via Playwright."""

from __future__ import annotations

import os
import time
from typing import Optional

from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.core import (
    _get_base_url,
    _launch_browser,
    _new_context,
)
from inspire.platform.web.session import WebSession, get_web_session

from ..jupyter import build_jupyter_proxy_url, open_notebook_lab
from .commands import build_rtunnel_setup_commands
from .probe import probe_existing_rtunnel_proxy_url
from .state import save_rtunnel_proxy_state
from .verify import wait_for_rtunnel_reachable


def _timing_enabled() -> bool:
    value = os.environ.get("INSPIRE_RTUNNEL_TIMING", "")
    return value.strip().lower() in {"1", "true", "yes"}


def _build_vscode_proxy_url(page, *, port: int) -> str | None:  # noqa: ANN001
    from urllib.parse import parse_qs, urlparse

    vscode_url = None
    for frame in page.frames:
        if "/vscode/" in (frame.url or ""):
            vscode_url = frame.url
            break
    if not vscode_url:
        return None

    parsed = urlparse(vscode_url)
    token = parse_qs(parsed.query).get("token", [None])[0]
    base = vscode_url.split("?", 1)[0].rstrip("/")
    proxy_url = f"{base}/proxy/{port}/"
    if token:
        proxy_url = f"{proxy_url}?token={token}"
    return proxy_url


def _extract_probe_error_summary(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        return error.__class__.__name__

    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return error.__class__.__name__

    headline = lines[0]
    last_response = next((line for line in lines if line.startswith("Last response:")), "")
    if last_response:
        return f"{headline}; {last_response}"
    return headline


def _ensure_proxy_readiness_with_fallback(
    *,
    proxy_url: str,
    port: int,
    timeout: int,
    context,  # noqa: ANN001
    page,  # noqa: ANN001
) -> tuple[str, list[str]]:
    import sys as _sys

    diagnostics: list[str] = []
    primary_verify_timeout_s = max(12, min(timeout, 35))

    try:
        wait_for_rtunnel_reachable(
            proxy_url=proxy_url,
            timeout_s=primary_verify_timeout_s,
            context=context,
            page=page,
        )
        return proxy_url, diagnostics
    except Exception as primary_error:
        diagnostics.append(f"primary={_extract_probe_error_summary(primary_error)}")

    fallback_proxy_url = _build_vscode_proxy_url(page, port=port)
    if not fallback_proxy_url:
        try:
            vscode_tab = page.locator('img[alt="vscode"]').first
            if vscode_tab.count() > 0:
                vscode_tab.click(timeout=1500)
                page.wait_for_timeout(200)
        except Exception:
            pass
        fallback_proxy_url = _build_vscode_proxy_url(page, port=port)

    if not fallback_proxy_url or fallback_proxy_url == proxy_url:
        _sys.stderr.write(
            "  Primary proxy did not pass HTTP readiness; "
            "continuing with SSH preflight.\n"
        )
        _sys.stderr.flush()
        return proxy_url, diagnostics

    _sys.stderr.write(f"  Primary proxy failed; retrying via: {fallback_proxy_url}\n")
    _sys.stderr.flush()
    try:
        wait_for_rtunnel_reachable(
            proxy_url=fallback_proxy_url,
            timeout_s=max(12, min(timeout, 45)),
            context=context,
            page=page,
        )
        return fallback_proxy_url, diagnostics
    except Exception as fallback_error:
        diagnostics.append(f"fallback={_extract_probe_error_summary(fallback_error)}")
        _sys.stderr.write(
            "  Fallback proxy did not pass HTTP readiness; "
            "continuing with SSH preflight.\n"
        )
        _sys.stderr.flush()
        return proxy_url, diagnostics


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

    timing = _timing_enabled()
    started_at = time.monotonic()

    if session is None:
        session = get_web_session()
    account = session.login_username

    existing = probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=port,
        session=session,
        account=account,
    )
    if existing:
        if timing:
            _sys.stderr.write(f"  Timing: fast-path probe {time.monotonic() - started_at:.2f}s\n")
            _sys.stderr.flush()
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
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=45000)
            except Exception:
                pass

            try:
                lab_frame.locator(
                    "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                ).first.wait_for(
                    state="visible",
                    timeout=45000,
                )
            except Exception:
                try:
                    lab_frame.get_by_role("menuitem", name="File").first.wait_for(
                        state="visible",
                        timeout=45000,
                    )
                except Exception:
                    lab_frame.get_by_role("menuitem", name="文件").first.wait_for(
                        state="visible",
                        timeout=45000,
                    )

            # Dismiss any popups (Jupyter news, jupyterlab-git, etc.)
            for _pass in range(1):
                dismissed = False
                for label in ("Dismiss", "No", "否", "不接收", "取消"):
                    try:
                        btn = lab_frame.get_by_role("button", name=label)
                        if btn.count() > 0:
                            btn.first.click(timeout=1000)
                            dismissed = True
                            break
                    except Exception:
                        pass
                if not dismissed:
                    # Also try closing via X button on dialog
                    try:
                        close_btn = lab_frame.locator("button.jp-Dialog-close, button[aria-label='Close']")
                        if close_btn.count() > 0:
                            close_btn.first.click(timeout=1000)
                            dismissed = True
                    except Exception:
                        pass
                if dismissed:
                    page.wait_for_timeout(150)
                else:
                    break

            terminal_opened = False

            # Check if a terminal tab is already open (e.g. from keepalive script)
            try:
                existing_term = lab_frame.locator(
                    "li.lm-TabBar-tab:has-text('Terminal'), li.lm-TabBar-tab:has-text('终端')"
                ).first
                if existing_term.count() > 0:
                    existing_term.click(timeout=2000)
                    page.wait_for_timeout(150)
                    terminal_opened = True
            except Exception:
                pass

            if not terminal_opened:
                terminal_card = lab_frame.locator(
                    "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                )
                try:
                    terminal_card.first.wait_for(state="visible", timeout=8000)
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
                        launcher_btn.click(timeout=1200)
                        page.wait_for_timeout(150)
                    terminal_card = lab_frame.locator(
                        "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                    )
                    terminal_card.first.wait_for(state="visible", timeout=8000)
                    terminal_card.first.click(timeout=8000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            if not terminal_opened:
                try:
                    try:
                        lab_frame.get_by_role("menuitem", name="File").first.click(timeout=2000)
                        lab_frame.get_by_role("menuitem", name="New").first.hover(timeout=2000)
                        lab_frame.get_by_role("menuitem", name="Terminal").first.click(timeout=3000)
                    except Exception:
                        lab_frame.get_by_role("menuitem", name="文件").first.click(timeout=2000)
                        lab_frame.get_by_role("menuitem", name="新建").first.hover(timeout=2000)
                        lab_frame.get_by_role("menuitem", name="终端").first.click(timeout=3000)
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
                    page.wait_for_timeout(80)
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

            # Dismiss any popups that appeared during terminal opening
            for label in ("Dismiss", "No", "否", "不接收", "取消"):
                try:
                    btn = lab_frame.get_by_role("button", name=label)
                    if btn.count() > 0:
                        btn.first.click(timeout=1000)
                        page.wait_for_timeout(120)
                        break
                except Exception:
                    pass

            # Re-focus terminal after popup dismissal
            try:
                term_focus = lab_frame.locator(
                    "textarea.xterm-helper-textarea"
                ).first
                if term_focus.count() > 0:
                    term_focus.click(timeout=2000)
            except Exception:
                pass

            # Wait for terminal to be ready
            page.wait_for_timeout(120)

            cmd_lines = build_rtunnel_setup_commands(
                port=port,
                ssh_port=ssh_port,
                ssh_public_key=ssh_public_key,
                ssh_runtime=ssh_runtime,
            )

            total_chars = sum(len(line) for line in cmd_lines)
            _sys.stderr.write(
                f"  Executing {len(cmd_lines)} setup commands "
                f"({total_chars} chars) in notebook terminal...\n"
            )
            _sys.stderr.flush()
            for line in cmd_lines:
                page.keyboard.insert_text(line)
                page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            try:
                page.screenshot(path="/tmp/notebook_terminal_debug.png")
            except Exception:
                pass

            proxy_url = jupyter_proxy_url
            _sys.stderr.write(f"  Verifying rtunnel is reachable at: {proxy_url}\n")
            _sys.stderr.flush()
            proxy_url, probe_diagnostics = _ensure_proxy_readiness_with_fallback(
                proxy_url=proxy_url,
                port=port,
                timeout=timeout,
                context=context,
                page=page,
            )
            if probe_diagnostics:
                _sys.stderr.write(
                    "  Proxy readiness summary: " + " | ".join(probe_diagnostics) + "\n"
                )
                _sys.stderr.flush()

            try:
                save_rtunnel_proxy_state(
                    notebook_id=notebook_id,
                    proxy_url=proxy_url,
                    port=port,
                    ssh_port=ssh_port,
                    base_url=_get_base_url(),
                    account=account,
                )
            except Exception:
                pass

            if timing:
                _sys.stderr.write(
                    f"  Timing: browser setup total {time.monotonic() - started_at:.2f}s\n"
                )
                _sys.stderr.flush()
            return proxy_url

        finally:
            try:
                context.close()
            finally:
                browser.close()
