"""Web session management using Playwright for accurate GPU availability data.

Uses browser automation to login and capture session cookies,
then makes direct API calls with those cookies.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests

# Session cache file
SESSION_CACHE_FILE = Path.home() / ".cache" / "inspire-cli" / "web_session.json"
SESSION_TTL = 3600  # 1 hour


class SessionExpiredError(Exception):
    """Raised when the web session has expired (401 from server)."""
    pass

# Default workspace (most commonly used - has 1388 nodes)
DEFAULT_WORKSPACE_ID = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"


def get_playwright_proxy() -> Optional[dict]:
    proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy:
        return {"server": proxy}
    return None


@dataclass
class WebSession:
    """Captured web session for web-ui APIs.

    We store Playwright `storage_state` because the web-ui APIs behind `/api/v1/*`
    are protected by Keycloak/CAS SSO and can require more than just a couple
    of cookies.
    """

    storage_state: dict[str, Any]
    created_at: float
    workspace_id: Optional[str] = None

    # Back-compat: older cache stored only name->value cookies
    cookies: Optional[dict[str, str]] = None

    def is_valid(self) -> bool:
        """Check if session is still valid (not expired)."""
        return (time.time() - self.created_at) < SESSION_TTL

    def to_dict(self) -> dict:
        return {
            "storage_state": self.storage_state,
            "cookies": self.cookies,
            "workspace_id": self.workspace_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WebSession":
        # Back-compat with older cache files that stored only cookies
        storage_state = data.get("storage_state")
        cookies = data.get("cookies")
        if storage_state is None:
            storage_state = {"cookies": [], "origins": []}
        return cls(
            storage_state=storage_state,
            cookies=cookies,
            workspace_id=data.get("workspace_id"),
            created_at=data["created_at"],
        )

    def save(self) -> None:
        """Save session to cache file."""
        SESSION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Restrict permissions: session contains sensitive cookies/tokens.
        tmp_path = SESSION_CACHE_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp_path, SESSION_CACHE_FILE)
        try:
            os.chmod(SESSION_CACHE_FILE, 0o600)
        except Exception:
            pass

    @classmethod
    def load(cls, allow_expired: bool = False) -> Optional["WebSession"]:
        """Load session from cache file if valid.

        Args:
            allow_expired: If True, return session even if TTL has expired.
                          The session cookies may still be valid server-side.
        """
        if not SESSION_CACHE_FILE.exists():
            return None
        try:
            with open(SESSION_CACHE_FILE) as f:
                data = json.load(f)
            session = cls.from_dict(data)
            if allow_expired or session.is_valid():
                return session
        except (json.JSONDecodeError, KeyError):
            pass
        return None


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
    base_url: str = "https://qz.sii.edu.cn",
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

        # Try different login form selectors (CAS vs Keycloak vs qz.sii.edu.cn)
        # The login page may redirect to CAS which has different form fields
        selectors_to_try = [
            ('input[placeholder="Username/alias"]', 'input[placeholder="Password"]'),
            ('input[name="username"]', 'input[name="password"]'),
            ('input#username', 'input#passwordShow'),  # CAS login with visible password field
            ('input#username', 'input#password'),
        ]

        username_filled = False
        password_selector = None
        for user_sel, pass_sel in selectors_to_try:
            try:
                page.wait_for_selector(user_sel, timeout=5000)
                page.locator(user_sel).first.fill(username)
                page.locator(pass_sel).first.fill(password)
                password_selector = pass_sel
                username_filled = True
                break
            except Exception:
                continue

        if not username_filled:
            # Fallback: try clicking "Account login" first, then use original selectors
            try:
                page.get_by_text("Account login", exact=True).click(timeout=5000, force=True)
                page.wait_for_timeout(500)
                page.wait_for_selector('input[placeholder="Username/alias"]', timeout=20000)
                page.locator('input[placeholder="Username/alias"]').first.fill(username)
                page.locator('input[placeholder="Password"]').first.fill(password)
                password_selector = 'input[placeholder="Password"]'
                username_filled = True
            except Exception:
                raise ValueError("Could not find login form on page")

        # Submit the form
        try:
            page.locator('button[type="submit"]').first.click(timeout=5000)
        except Exception:
            try:
                page.locator('input[type="submit"]').first.click(timeout=5000)
            except Exception:
                if password_selector:
                    page.locator(password_selector).first.press("Enter")

        # Wait for SSO redirects to complete (CAS -> Keycloak -> qz)
        start = time.time()
        while time.time() - start < 30:
            page.wait_for_timeout(500)
            if "qz.sii.edu.cn" in page.url and "keycloak" not in page.url and "cas" not in page.url:
                break

        # Visit a real page to ensure app session cookies are set.
        # Use domcontentloaded with fallback since some pages have long-polling.
        try:
            page.goto(f"{base_url}/jobs/distributedTraining", wait_until="networkidle", timeout=15000)
        except Exception:
            page.goto(f"{base_url}/jobs/distributedTraining", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)

        # Extract workspace_id (spaceId)
        # Priority: 1) env var override, 2) default workspace, 3) auto-detect from browser
        workspace_id = os.environ.get('INSPIRE_WORKSPACE_ID')

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
            created_at=time.time()
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
    env_workspace_id = os.environ.get('INSPIRE_WORKSPACE_ID')

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
    return login_with_playwright(username, password)


def fetch_node_specs(
    session: WebSession,
    compute_group_id: str,
    base_url: str = "https://qz.sii.edu.cn",
) -> dict:
    """Fetch detailed node specs for a compute group using web session.

    This API returns per-GPU task information via node_dimensions.

    Note: This endpoint is a web UI internal API that requires Keycloak
    authentication. We use Playwright's browser context to make the request
    with proper session handling, including cookies and browser state.
    """
    from playwright.sync_api import sync_playwright

    if not session.storage_state or not session.storage_state.get("cookies"):
        raise ValueError("Session expired or invalid (missing storage state)")

    url = f"{base_url}/api/v1/compute_resources/node_specs/logic_compute_groups/{compute_group_id}"

    proxy = get_playwright_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy=proxy)
        context = browser.new_context(storage_state=session.storage_state, proxy=proxy, ignore_https_errors=True)

        try:
            resp = context.request.get(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{base_url}/jobs/distributedTraining",
                },
                timeout=30000,
            )

            if resp.status == 401:
                raise ValueError("Session expired or invalid")
            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            return resp.json()

        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()


def fetch_workspace_availability(
    session: WebSession,
    base_url: str = "https://qz.sii.edu.cn",
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Fetch workspace-specific GPU availability.

    Uses the browser API endpoint that returns nodes filtered by workspace.
    This matches what the user sees in the browser.

    Args:
        session: Web session with storage_state and workspace_id
        base_url: Base URL for the API
        progress_callback: Optional callback(fetched, total) for progress updates

    Returns:
        List of node dictionaries with availability info.

    Raises:
        ValueError: If session is invalid or workspace_id is missing.
    """
    from playwright.sync_api import sync_playwright

    if not session.storage_state or not session.storage_state.get("cookies"):
        raise ValueError("Session expired or invalid (missing storage state)")

    if not session.workspace_id:
        raise ValueError("No workspace_id in session. Please login again.")

    url = f"{base_url}/api/v1/cluster_nodes/workspace/{session.workspace_id}"

    proxy = get_playwright_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy=proxy)
        context = browser.new_context(storage_state=session.storage_state, proxy=proxy, ignore_https_errors=True)

        try:
            resp = context.request.get(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{base_url}/jobs/distributedTraining",
                },
                timeout=30000,
            )

            if resp.status == 401:
                raise ValueError("Session expired or invalid")
            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            data = resp.json()
            return data.get("data", {}).get("nodes", [])

        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()


@dataclass
class GPUAvailability:
    """Per-GPU availability for a compute group."""
    group_id: str
    group_name: str
    gpu_type: str
    total_gpus: int
    free_gpus: int
    low_priority_gpus: int  # GPUs used by low-priority tasks


def fetch_gpu_availability(
    session: WebSession,
    compute_group_ids: list[str],
    base_url: str = "https://qz.sii.edu.cn",
) -> list[GPUAvailability]:
    """Fetch accurate per-GPU availability for compute groups."""
    results = []

    for group_id in compute_group_ids:
        try:
            data = fetch_node_specs(session, group_id, base_url)

            # Parse the response to count free GPUs
            nodes = data.get("data", {}).get("node_dimensions", [])

            total_gpus = 0
            free_gpus = 0
            low_priority_gpus = 0
            group_name = ""
            gpu_type = ""

            for node in nodes:
                gpu_count = node.get("gpu_count", 8)
                total_gpus += gpu_count

                # Check tasks_associated for each GPU dimension
                tasks = node.get("tasks_associated", [])
                if not tasks:
                    free_gpus += gpu_count
                else:
                    # Check if tasks are low priority
                    for task in tasks:
                        priority = task.get("priority", 10)
                        if priority < 5:  # Low priority threshold
                            low_priority_gpus += 1

                if not group_name:
                    group_name = node.get("logic_compute_group_name", "Unknown")
                if not gpu_type:
                    gpu_info = node.get("gpu_info", {})
                    gpu_type = gpu_info.get("gpu_type_display", "Unknown")

            results.append(GPUAvailability(
                group_id=group_id,
                group_name=group_name,
                gpu_type=gpu_type,
                total_gpus=total_gpus,
                free_gpus=free_gpus,
                low_priority_gpus=low_priority_gpus,
            ))

        except Exception as e:
            # Skip groups that fail
            print(f"Warning: Failed to fetch {group_id}: {e}")
            continue

    return results


def clear_session_cache() -> None:
    """Clear the cached web session."""
    if SESSION_CACHE_FILE.exists():
        SESSION_CACHE_FILE.unlink()
