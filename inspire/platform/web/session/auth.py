"""Authentication helpers for web-session based APIs."""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from inspire.config import AccountContext, Config
from inspire.platform import tls_policy

from .browser import (
    MISSING_BROWSER_HINT,
    PLAYWRIGHT_INSTALL_HINT,
    MissingBrowserError,
)
from .http_auth import (
    _classify_cas_login_error,
    _extract_workspaces,
    login_with_requests,
)
from .models import (
    DEFAULT_WORKSPACE_ID,
    SessionAuthenticationError,
    WebSession,
    get_auth_failure_cache_file,
)
from .proxy import get_playwright_proxy


def _load_runtime_config(account_alias: str | None = None) -> Config:
    config, _ = Config.from_files_and_env(
        require_credentials=False,
        require_target_dir=False,
        account_alias=account_alias,
    )
    return config


def _session_matches_account_context(
    cached: WebSession,
    account_context: AccountContext,
) -> bool:
    if not account_context.username:
        return False
    cached_alias = str(cached.account_alias or "").strip() or None
    if cached_alias != account_context.alias:
        return False
    if str(cached.login_username or "").strip() != account_context.username:
        return False
    cached_base_url = str(cached.base_url or "").rstrip("/")
    return not cached_base_url or cached_base_url == account_context.base_url


def _has_real_workspace_id(session: WebSession) -> bool:
    value = str(session.workspace_id or "").strip()
    return bool(value) and value != DEFAULT_WORKSPACE_ID


def _auth_failure_cooldown_seconds(message: str) -> int:
    normalized = str(message or "").lower()
    if "locked" in normalized or "锁定" in normalized:
        return 30 * 60
    return 5 * 60


def _record_auth_failure(account: str, message: str) -> None:
    path = get_auth_failure_cache_file(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = {
        "schema": "inspire.web-auth-failure.v1",
        "message": str(message or "CAS authentication failed"),
        "created_at": now,
        "retry_after": now + _auth_failure_cooldown_seconds(message),
    }
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload) + "\n")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _clear_auth_failure(account: str) -> None:
    try:
        get_auth_failure_cache_file(account).unlink()
    except FileNotFoundError:
        pass


def _raise_during_auth_cooldown(account: str) -> None:
    path = get_auth_failure_cache_file(account)
    try:
        payload = json.loads(path.read_text())
        retry_after = float(payload["retry_after"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return
    remaining = retry_after - time.time()
    if remaining <= 0:
        _clear_auth_failure(account)
        return
    message = str(payload.get("message") or "CAS authentication failed")
    wait_seconds = max(1, int(remaining + 0.999))
    raise SessionAuthenticationError(
        f"CAS authentication paused for this account for {wait_seconds}s after: {message}"
    )


def _maybe_apply_workspace_override(
    cached: WebSession,
    env_workspace_id: Optional[str],
    cache_key: str,
) -> None:
    if not env_workspace_id:
        return
    if cached.workspace_id == env_workspace_id:
        return
    cached.workspace_id = env_workspace_id
    try:
        cached.save(account=cache_key)
    except Exception:
        pass


def _get_auth_method(config: Config | None = None) -> str:
    """Determine authentication method from config or env."""
    env_method = os.environ.get("INSPIRE_AUTH_METHOD", "").strip().lower()
    if env_method in ("http", "playwright"):
        return env_method
    try:
        runtime_config = config or _load_runtime_config()
        method = (runtime_config.auth_method or "http").strip().lower()
        if method in ("http", "playwright"):
            return method
    except Exception:
        pass
    return "http"


def get_credentials() -> tuple[str, str]:
    """Get web credentials from layered config (project/global/env/default)."""
    config = _load_runtime_config()
    username = (config.username or "").strip()
    password = config.password or ""

    if not username or not password:
        raise ValueError(
            "Missing web authentication credentials. Select an account alias and configure "
            'its username/password under [accounts."<alias>"], or use the legacy '
            "INSPIRE_USERNAME/INSPIRE_PASSWORD settings."
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
    from inspire.platform.web.browser_api.core import (
        _in_asyncio_loop,
        _launch_browser,
        _run_in_thread,
    )

    if _in_asyncio_loop():
        return _run_in_thread(
            login_with_playwright,
            username,
            password,
            base_url=base_url,
            headless=headless,
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            f"{PLAYWRIGHT_INSTALL_HINT}\nOr switch to HTTP auth: export INSPIRE_AUTH_METHOD=http"
        ) from None

    proxy = get_playwright_proxy()
    with sync_playwright() as p:
        try:
            # Shared launcher: headless containers need the no-sandbox flags, and a
            # missing Chromium binary is reported as an install step rather than a
            # raw Playwright traceback.
            browser = _launch_browser(p, headless=headless)
        except MissingBrowserError:
            raise MissingBrowserError(
                "Playwright browser not found. First login requires a browser to "
                f"complete SSO authentication.\n\n{MISSING_BROWSER_HINT}"
            ) from None
        # The login flow types the password into this context and ends up holding CAS/Keycloak
        # cookies, so it verifies certificates by default; True only via api.skip_ssl_verify,
        # which warns on stderr.
        context = browser.new_context(
            proxy=proxy, ignore_https_errors=tls_policy.ignore_https_errors()
        )
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
                pass_locator.evaluate(
                    """
                    el => {
                      const btn = el.form?.querySelector('#passbutton,button[type="submit"],input[type="submit"]');
                      if (btn) { btn.click(); return true; }
                      return false;
                    }
                    """
                )
            except Exception:
                pass

        def _raise_if_cas_login_error() -> None:
            try:
                if "cas.sii.edu.cn" not in page.url:
                    return
                body_text = page.locator("body").inner_text(timeout=2000)
            except Exception:
                return

            login_error = _classify_cas_login_error(body_text)
            if login_error:
                raise SessionAuthenticationError(login_error)

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
            page.wait_for_timeout(1000)
            _raise_if_cas_login_error()

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
        _raise_if_cas_login_error()

        def _wait_for_api_auth() -> None:
            deadline = time.time() + 30
            headers = {
                "Accept": "application/json",
                "Referer": f"{base_url}/jobs/distributedTraining",
            }
            while time.time() < deadline:
                try:
                    # v2 Action API: /api/v1 is retired (#82). A failure arrives as HTTP 200
                    # with ResponseMetadata.Error, so a 200 alone is not proof of a session.
                    resp = context.request.post(
                        f"{base_url}/api/v2/user?Action=GetUserDetail",
                        headers=headers,
                        data={},
                        timeout=10000,
                    )
                    if resp.status == 200:
                        payload = resp.json()
                        error = ((payload.get("ResponseMetadata") or {}).get("Error")) or {}
                        if not error:
                            return
                except Exception:
                    pass
                _raise_if_cas_login_error()
                page.wait_for_timeout(500)
            _raise_if_cas_login_error()
            raise ValueError("Login did not complete; check credentials")

        _wait_for_api_auth()

        # Extract workspace_id (spaceId)
        # Priority: 1) env var override, 2) auto-detect from browser, 3) default placeholder
        workspace_id = os.environ.get("INSPIRE_WORKSPACE_ID", "").strip()
        if not workspace_id:
            try:
                detected = page.evaluate("() => window.localStorage.getItem('spaceId')")
            except Exception:
                detected = None

            detected_str = str(detected or "").strip()
            if detected_str:
                workspace_id = detected_str

        if not workspace_id:
            workspace_id = DEFAULT_WORKSPACE_ID

        all_workspace_ids: list[str] = []
        all_workspace_names: dict[str, str] = {}
        try:
            workspaces_resp = context.request.post(
                f"{base_url}/api/v2/workspace?Action=ListWorkspaces",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Referer": f"{base_url}/jobs/distributedTraining",
                },
                data={},
                timeout=15000,
            )
            if workspaces_resp.status == 200:
                all_workspace_ids, all_workspace_names = _extract_workspaces(workspaces_resp.json())
        except Exception:
            pass

        if workspace_id == DEFAULT_WORKSPACE_ID and all_workspace_ids:
            workspace_id = all_workspace_ids[0]
        if workspace_id and workspace_id != DEFAULT_WORKSPACE_ID:
            if workspace_id not in all_workspace_ids:
                all_workspace_ids.insert(0, workspace_id)

        # Capture storage state (cookies + localStorage)
        storage_state = context.storage_state()

        # Keep a simple cookie name->value mapping for debugging/back-compat
        cookies = context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        context.close()
        browser.close()
        # Give Playwright a moment to finish background cleanup tasks
        # before the sync_playwright greenlet exits.
        time.sleep(0.1)

        session = WebSession(
            storage_state=storage_state,
            cookies=cookie_dict,
            workspace_id=workspace_id,
            login_username=username,
            base_url=base_url,
            all_workspace_ids=all_workspace_ids or None,
            all_workspace_names=all_workspace_names or None,
            created_at=time.time(),
        )
        return session


def get_web_session(
    force_refresh: bool = False,
    require_workspace: bool = False,
    account: Optional[str] = None,
) -> WebSession:
    """Get a valid web session, logging in if necessary.

    Args:
        force_refresh: Force a new login even if cached session exists.
        require_workspace: Force re-login if workspace_id is missing.
        account: Optional account alias. The alias is resolved to platform credentials.

    Returns:
        A valid WebSession with storage_state and optionally workspace_id.
    """
    # Check for workspace override from environment
    env_workspace_id = os.environ.get("INSPIRE_WORKSPACE_ID", "").strip()

    config = _load_runtime_config(account_alias=account) if account else _load_runtime_config()
    account_context = config.get_account_context()
    lookup_account = account_context.cache_key or None
    credentials_error: Optional[ValueError] = None
    if not account_context.username or not account_context.password:
        credentials_error = ValueError(
            "Missing web authentication credentials for "
            f"account {account_context.alias or account_context.username or '<unselected>'}."
        )

    if not force_refresh:
        cached = WebSession.load(account=lookup_account)
        if (
            cached
            and cached.storage_state.get("cookies")
            and _session_matches_account_context(cached, account_context)
        ):
            _maybe_apply_workspace_override(cached, env_workspace_id, account_context.cache_key)
            if require_workspace and not _has_real_workspace_id(cached):
                pass
            else:
                return cached

    # If we can't refresh (missing credentials), try the cached session anyway.
    if credentials_error is not None:
        cached = WebSession.load(allow_expired=True, account=lookup_account)
        if (
            cached
            and cached.storage_state.get("cookies")
            and _session_matches_account_context(cached, account_context)
        ):
            _maybe_apply_workspace_override(cached, env_workspace_id, account_context.cache_key)

            if not require_workspace or _has_real_workspace_id(cached):
                return cached
        raise credentials_error

    # Use cached session if available and has cookies, even if beyond TTL.
    # The session cookies may still be valid server-side; let API calls determine validity.
    # Skip this when force_refresh is set — the caller explicitly wants a fresh login.
    if not force_refresh:
        cached = WebSession.load(allow_expired=True, account=lookup_account)
        if (
            cached
            and cached.storage_state.get("cookies")
            and _session_matches_account_context(cached, account_context)
        ):
            _maybe_apply_workspace_override(cached, env_workspace_id, account_context.cache_key)
            if not require_workspace or _has_real_workspace_id(cached):
                # Use cached session; server will reject if truly invalid.
                return cached

    # Session is missing or has no cookies, perform fresh login
    _raise_during_auth_cooldown(account_context.cache_key)
    auth_method = _get_auth_method(config)
    try:
        if auth_method == "http":
            session = login_with_requests(
                account_context.username,
                account_context.password,
                base_url=account_context.base_url,
            )
        else:
            session = login_with_playwright(
                account_context.username,
                account_context.password,
                base_url=account_context.base_url,
            )
    except SessionAuthenticationError as exc:
        _record_auth_failure(account_context.cache_key, str(exc))
        raise

    _clear_auth_failure(account_context.cache_key)

    session.account_alias = account_context.alias
    if str(session.login_username or "").strip() != account_context.username:
        raise ValueError(
            "Authenticated session username does not match configured platform identity: "
            f"expected {account_context.username!r}, got {session.login_username!r}."
        )
    session.base_url = account_context.base_url
    session.save(account=account_context.cache_key)
    return session
