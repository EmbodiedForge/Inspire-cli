"""Browser (web-session) notebook APIs (Playwright flows).

This module contains browser automation used for rtunnel setup and running commands.
"""

from __future__ import annotations

import os
from typing import Optional

from inspire.cli.utils.browser_api_core import (
    BASE_URL,
    _browser_api_path,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.cli.utils.web_session import WebSession, build_requests_session, get_web_session

__all__ = [
    "run_command_in_notebook",
    "setup_notebook_rtunnel",
]


def setup_notebook_rtunnel(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Ensure the notebook exposes an rtunnel server via Jupyter proxy."""
    if _in_asyncio_loop():
        return _run_in_thread(
            _setup_notebook_rtunnel_sync,
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            session=session,
            headless=headless,
            timeout=timeout,
        )
    return _setup_notebook_rtunnel_sync(
        notebook_id=notebook_id,
        port=port,
        ssh_port=ssh_port,
        ssh_public_key=ssh_public_key,
        session=session,
        headless=headless,
        timeout=timeout,
    )


def _setup_notebook_rtunnel_sync(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Sync implementation for setup_notebook_rtunnel."""
    import shlex
    import sys as _sys
    import time

    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    notebook_lab_path = _browser_api_path(f"/notebook/lab/{notebook_id}/proxy/{port}/")
    known_proxy_url = f"{BASE_URL}{notebook_lab_path}"
    try:
        http = build_requests_session(session, BASE_URL)
        resp = http.get(known_proxy_url, timeout=5)
        body = resp.text[:200] if resp.text else ""
        if resp.status_code == 200 and "ECONNREFUSED" not in body and "<html>" not in body.lower():
            _sys.stderr.write("Using existing rtunnel connection (fast path).\n")
            _sys.stderr.flush()
            http.close()
            return known_proxy_url
        http.close()
    except Exception:
        pass

    _sys.stderr.write("Setting up rtunnel tunnel via browser automation...\n")
    _sys.stderr.flush()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()

        try:
            page.goto(
                f"{BASE_URL}/ide?notebook_id={notebook_id}",
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
                direct_lab_url = f"{BASE_URL}{notebook_lab_prefix}/{notebook_id}/"
                page.goto(
                    direct_lab_url,
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
                lab_frame = page

            jupyter_url = lab_frame.url
            notebook_lab_pattern = _browser_api_path("/notebook/lab/")
            if notebook_lab_pattern.lstrip("/") in jupyter_url:
                from urllib.parse import urlsplit, urlunsplit

                parsed = urlsplit(jupyter_url)
                base_path = parsed.path
                if not base_path.endswith("/"):
                    base_path = base_path + "/"
                base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
                jupyter_proxy_url = f"{base_url}proxy/{port}/"
            else:
                jupyter_proxy_url = jupyter_url.rstrip("/")
                if jupyter_proxy_url.endswith("/lab"):
                    jupyter_proxy_url = jupyter_proxy_url[:-4]
                jupyter_proxy_url = f"{jupyter_proxy_url}/proxy/{port}/"

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
                    "textarea.xterm-helper-textarea, textarea.xterm-helper-textarea, div.xterm-helper-textarea textarea"
                ).first
                if term_focus.count() > 0:
                    term_focus.click(timeout=2000)
            except Exception:
                pass

            if ssh_public_key:
                ssh_public_key_escaped = ssh_public_key.replace("'", "'\"'\"'")
                key_line = f"mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo '{ssh_public_key_escaped}' >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys"
            else:
                key_line = "mkdir -p /root/.ssh && chmod 700 /root/.ssh"

            # Allow providing a local rtunnel binary (avoids network downloads on no-internet nodes)
            rtunnel_bin = os.environ.get("INSPIRE_RTUNNEL_BIN")
            sshd_deb_dir = os.environ.get("INSPIRE_SSHD_DEB_DIR")
            dropbear_deb_dir = os.environ.get("INSPIRE_DROPBEAR_DEB_DIR")

            # Determine rtunnel download URL (best-effort)
            try:
                from inspire.cli.utils.tunnel import _get_rtunnel_download_url

                rtunnel_download_url = _get_rtunnel_download_url()
            except Exception:
                rtunnel_download_url = "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz"

            cmd_lines = [
                f"PORT={port}",
                f"SSH_PORT={ssh_port}",
                key_line,
            ]

            if rtunnel_bin:
                cmd_lines.append(f"RTUNNEL_BIN_PATH={shlex.quote(rtunnel_bin)}")
                cmd_lines.append(
                    'if [ -f "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi'
                )

            if sshd_deb_dir:
                cmd_lines.append(f"SSHD_DEB_DIR={shlex.quote(sshd_deb_dir)}")
            if dropbear_deb_dir:
                cmd_lines.append(f"DROPBEAR_DEB_DIR={shlex.quote(dropbear_deb_dir)}")

            if dropbear_deb_dir:
                setup_script = os.environ.get("INSPIRE_SETUP_SCRIPT")
                if not setup_script:
                    raise ValueError(
                        "INSPIRE_SETUP_SCRIPT environment variable is required when using dropbear. "
                        "It should point to a script that installs dropbear and starts rtunnel."
                    )
                rtunnel_bin_arg = shlex.quote(rtunnel_bin) if rtunnel_bin else ""
                cmd_lines.append(
                    f"bash {shlex.quote(setup_script)} {shlex.quote(dropbear_deb_dir)} {shlex.quote(rtunnel_bin_arg)} \"$SSH_PORT\" \"$PORT\" >/tmp/setup_ssh.log 2>&1; tail -80 /tmp/setup_ssh.log; echo '>>> dropbear log'; tail -60 /tmp/dropbear.log 2>/dev/null || true; echo '>>> rtunnel log'; tail -60 /tmp/rtunnel-server.log 2>/dev/null || true"
                )
            else:
                cmd_lines.extend(
                    [
                        f"RTUNNEL_URL={rtunnel_download_url!r}",
                        'if [ ! -x /usr/sbin/sshd ] && [ -z "${SSHD_DEB_DIR:-}" ]; then export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq openssh-server; fi',
                        "pkill -f 'sshd -p' 2>/dev/null || true",
                        'if [ -x /usr/sbin/sshd ]; then mkdir -p /run/sshd && chmod 0755 /run/sshd; ssh-keygen -A >/dev/null 2>&1 || true; /usr/sbin/sshd -p "$SSH_PORT" -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes -o PasswordAuthentication=no -o PubkeyAuthentication=yes >/dev/null 2>&1 & fi',
                        "RTUNNEL_BIN=/tmp/rtunnel",
                        'if [ -n "${RTUNNEL_BIN_PATH:-}" ] && [ -x "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi',
                        'pkill -f "rtunnel.*:$PORT" 2>/dev/null || true',
                        f"if [ ! -x \"$RTUNNEL_BIN\" ]; then curl -fsSL '{rtunnel_download_url}' -o /tmp/rtunnel.tgz && tar -xzf /tmp/rtunnel.tgz -C /tmp && chmod +x /tmp/rtunnel 2>/dev/null; fi",
                        'nohup "$RTUNNEL_BIN" "127.0.0.1:$SSH_PORT" "0.0.0.0:$PORT" >/tmp/rtunnel-server.log 2>&1 &',
                    ]
                )

            _sys.stderr.write("  Executing setup commands in terminal...\n")
            _sys.stderr.flush()
            for line in cmd_lines:
                page.keyboard.type(line, delay=2)
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)

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
            start = time.time()
            last_status = None
            last_progress_time = start
            while time.time() - start < timeout:
                elapsed = time.time() - start
                if time.time() - last_progress_time >= 30:
                    _sys.stderr.write(f"  Waiting for rtunnel... ({int(elapsed)}s elapsed)\n")
                    _sys.stderr.flush()
                    last_progress_time = time.time()
                try:
                    resp = context.request.get(proxy_url, timeout=5000)
                    body = ""
                    try:
                        body = resp.text()
                    except Exception:
                        body = ""
                    last_status = f"{resp.status} {body[:200].strip()}"
                    if "ECONNREFUSED" not in body:
                        return proxy_url
                except Exception as e:
                    last_status = str(e)

                page.wait_for_timeout(1000)

            error_msg = (
                f"rtunnel server did not become reachable within {timeout}s.\n"
                f"Last response: {last_status}\n\n"
                "Debugging hints:\n"
                "  1. Check if rtunnel binary is present: ls -la /tmp/rtunnel\n"
                "  2. Check rtunnel server log: cat /tmp/rtunnel-server.log\n"
                "  3. Check if sshd/dropbear is running: ps aux | grep -E 'sshd|dropbear'\n"
                "  4. Check dropbear log: cat /tmp/dropbear.log\n"
                "  5. Try running with --debug-playwright to see the browser\n"
                "  6. Screenshot saved to /tmp/notebook_terminal_debug.png"
            )
            raise ValueError(error_msg)

        finally:
            try:
                context.close()
            finally:
                browser.close()


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
    import time

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
            page.goto(
                f"{BASE_URL}/ide?notebook_id={notebook_id}",
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
                direct_lab_url = f"{BASE_URL}{notebook_lab_prefix}/{notebook_id}/"
                page.goto(
                    direct_lab_url,
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
                lab_frame = page

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
                    "textarea.xterm-helper-textarea, textarea.xterm-helper-textarea, div.xterm-helper-textarea textarea"
                ).first
                if term_focus.count() > 0:
                    term_focus.click(timeout=2000)
            except Exception:
                pass

            # Execute command
            page.keyboard.type(command, delay=2)
            page.keyboard.press("Enter")

            # Give it a moment to run
            page.wait_for_timeout(int(timeout * 1000))

        finally:
            try:
                context.close()
            finally:
                browser.close()
