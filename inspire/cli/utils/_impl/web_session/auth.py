"""Authentication helpers for web-session based APIs."""

from __future__ import annotations

import os
import time
from typing import Optional

from .models import DEFAULT_WORKSPACE_ID, WebSession
from .proxy import get_playwright_proxy


def get_credentials() -> tuple[str, str]:
    """Get web credentials from environment."""
    username = os.environ.get("INSPIRE_USERNAME")
    password = os.environ.get("INSPIRE_PASSWORD")

    if not username or not password:
        raise ValueError(
            "INSPIRE_USERNAME and INSPIRE_PASSWORD must be set in environment. "
            "These are used for web login to get accurate GPU availability."
        )

    return username, password


def login_with_playwright(
    username: str,
    password: str,
    base_url: str = "https://api.example.com",
    headless: bool = True,
) -> WebSession:
    """Login to Inspire web UI using Playwright and capture session storage state.

    The login flow: qz/login -> CAS (Keycloak broker) -> Keycloak -> qz.
    """
    from playwright.sync_api import sync_playwright

    proxy = get_playwright_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, proxy=proxy)
        context = browser.new_context(proxy=proxy, ignore_https_errors=True)
        page = context.new_page()

        # Navigate to login page; use domcontentloaded since CAS may have
        # long-polling resources that prevent networkidle from completing.
        page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=60000)
        # Give some time for any redirects to settle
        page.wait_for_timeout(2000)

        login_pairs = [
            ("input#username", "input#passwordShow"),
            ("input[name='username']", "input[name='password']"),
            ("input[placeholder='Username/alias']", "input[placeholder='Password']"),
        ]

        def _fill_login_form() -> Optional[object]:
            for user_sel, pass_sel in login_pairs:
                try:
                    page.wait_for_selector(user_sel, timeout=5000, state="visible")
                    page.wait_for_selector(pass_sel, timeout=5000, state="visible")
                    user_locator = page.locator(user_sel).first
                    pass_locator = page.locator(pass_sel).first
                    user_locator.fill(username)
                    pass_locator.fill(password)
                    return pass_locator
                except Exception:
                    continue
            return None

        def _submit_login_form(pass_locator) -> None:  # noqa: ANN001
            try:
                pass_locator.press("Enter", timeout=3000)
                return
            except Exception:
                pass
            try:
                pass_locator.evaluate("el => el.form && el.form.submit()")
                return
            except Exception:
                pass
            try:
                pass_locator.evaluate("""
                    el => {
                      const btn = el.form?.querySelector('#passbutton,button[type="submit"],input[type="submit"]');
                      if (btn) { btn.click(); return true; }
                      return false;
                    }
                    """)
            except Exception:
                pass

        pass_locator = _fill_login_form()
        if not pass_locator:
            try:
                page.get_by_text("Account login", exact=True).click(timeout=3000, force=True)
                page.wait_for_timeout(500)
            except Exception:
                pass
            pass_locator = _fill_login_form()

        if pass_locator:
            _submit_login_form(pass_locator)

        # Visit a real page to ensure app session cookies and localStorage are set.
        # Use domcontentloaded with fallback since some pages have long-polling.
        try:
            page.goto(
                f"{base_url}/jobs/distributedTraining", wait_until="networkidle", timeout=15000
            )
        except Exception:
            page.goto(
                f"{base_url}/jobs/distributedTraining", wait_until="domcontentloaded", timeout=30000
            )
        page.wait_for_timeout(1000)

        def _wait_for_api_auth() -> None:
            deadline = time.time() + 30
            headers = {
                "Accept": "application/json",
                "Referer": f"{base_url}/jobs/distributedTraining",
            }
            while time.time() < deadline:
                try:
                    resp = context.request.get(
                        f"{base_url}/api/v1/user/detail",
                        headers=headers,
                        timeout=10000,
                    )
                    if resp.status == 200:
                        return
                except Exception:
                    pass
                page.wait_for_timeout(500)
            raise ValueError("Login did not complete; check credentials")

        _wait_for_api_auth()

        # Extract workspace_id (spaceId)
        # Priority: 1) env var override, 2) default workspace, 3) auto-detect from browser
        workspace_id = os.environ.get("INSPIRE_WORKSPACE_ID")

        # Use default workspace unless explicitly overridden
        if not workspace_id:
            workspace_id = DEFAULT_WORKSPACE_ID

        # Capture storage state (cookies + localStorage)
        storage_state = context.storage_state()

        # Keep a simple cookie name->value mapping for debugging/back-compat
        cookies = context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        browser.close()

        session = WebSession(
            storage_state=storage_state,
            cookies=cookie_dict,
            workspace_id=workspace_id,
            created_at=time.time(),
        )
        session.save()

        return session


def get_web_session(force_refresh: bool = False, require_workspace: bool = False) -> WebSession:
    """Get a valid web session, logging in if necessary.

    Args:
        force_refresh: Force a new login even if cached session exists.
        require_workspace: Force re-login if workspace_id is missing.

    Returns:
        A valid WebSession with storage_state and optionally workspace_id.
    """
    if not force_refresh:
        cached = WebSession.load()
        if cached and cached.storage_state.get("cookies"):
            if require_workspace and not cached.workspace_id:
                # Need workspace_id, force re-login
                pass
            else:
                return cached

    # Check for workspace override from environment
    env_workspace_id = os.environ.get("INSPIRE_WORKSPACE_ID")

    # If we can't refresh (missing credentials), try the cached session anyway.
    try:
        username, password = get_credentials()
    except ValueError:
        cached = WebSession.load(allow_expired=True)
        if cached and cached.storage_state.get("cookies"):
            if env_workspace_id and cached.workspace_id != env_workspace_id:
                cached.workspace_id = env_workspace_id
                try:
                    cached.save()
                except Exception:
                    pass

            if require_workspace and not cached.workspace_id:
                raise
            return cached
        raise

    # Use cached session if available and has cookies, even if beyond TTL.
    # The session cookies may still be valid server-side; let API calls determine validity.
    cached = WebSession.load(allow_expired=True)
    if cached and cached.storage_state.get("cookies"):
        if env_workspace_id and cached.workspace_id != env_workspace_id:
            cached.workspace_id = env_workspace_id
            try:
                cached.save()
            except Exception:
                pass
        # Use cached session; server will reject if truly invalid
        return cached

    # Session is missing or has no cookies, perform fresh login
    base_url = os.environ.get("INSPIRE_BASE_URL", "https://api.example.com")
    return login_with_playwright(username, password, base_url=base_url)
