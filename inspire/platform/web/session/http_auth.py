"""Pure HTTP CAS authentication using requests (no Playwright required)."""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING, Any, Optional, cast
from urllib.parse import urlparse

import requests

from inspire.platform.web.session.models import (
    DEFAULT_WORKSPACE_ID,
    SessionAuthenticationError,
    WebSession,
)

from .requests import build_requests_session

if TYPE_CHECKING:
    from playwright.sync_api import StorageState

# Live-probed against the CAS browser form. Keep the UA and Client Hints
# coherent: CAS currently rejects the older UA-only request shape.
_CAS_CHROMIUM_MAJOR = "145"
_CAS_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) HeadlessChrome/{_CAS_CHROMIUM_MAJOR}.0.0.0 Safari/537.36"
)
_CAS_NAVIGATION_HEADERS = {
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": f'"Not_A Brand";v="99", "Chromium";v="{_CAS_CHROMIUM_MAJOR}"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Linux"',
}


def _classify_cas_login_error(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text or "").lower()
    if not normalized:
        return None

    locked_markers = (
        "account is locked",
        "账号已锁定",
        "账户已锁定",
        "账号被锁定",
        "账户被锁定",
    )
    if any(marker.lower() in normalized for marker in locked_markers):
        return "CAS account is locked; stop retrying and contact the administrator"

    wrong_password_markers = (
        "用户名或密码错误",
        "账号或密码错误",
        "账户或密码错误",
        "wrong username or password",
        "invalid username or password",
    )
    if any(marker.lower() in normalized for marker in wrong_password_markers):
        return "Wrong username or password"

    captcha_markers = (
        "请输入验证码",
        "请填写验证码",
        "验证码错误",
        "验证码不正确",
        "验证码不能为空",
        "验证码已失效",
        "验证码失效",
        "captcha required",
        "captcha is required",
        "enter captcha",
        "please enter captcha",
        "invalid captcha",
        "captcha incorrect",
        "captcha error",
        "verification code required",
        "verification code is required",
        "please enter verification code",
        "invalid verification code",
        "verification code incorrect",
    )
    if any(marker in normalized for marker in captcha_markers):
        return "CAPTCHA required, login manually in browser"

    return None


def _rsa_encrypt_password(password: str) -> str:
    """Encrypt password using the hardcoded CAS RSA public key."""

    def _hex2int(hex_string: str) -> int:
        hex_string = hex_string.strip()
        if hex_string.startswith(("0x", "0X")):
            hex_string = hex_string[2:]
        return int(hex_string, 16)

    def _int2hex(number: int, min_length: int = 0) -> str:
        hex_str = format(number, "x")
        if min_length > 0:
            hex_str = hex_str.zfill(min_length)
        return hex_str

    def _bi_high_index(n: int) -> int:
        if n == 0:
            return 0
        bit_length = n.bit_length()
        return (bit_length + 15) // 16 - 1

    def _encode_block(byte_array: list[int], start: int, chunk_size: int) -> int:
        block = 0
        digit_index = 0
        for k in range(start, start + chunk_size, 2):
            byte1 = byte_array[k] if k < len(byte_array) else 0
            byte2 = byte_array[k + 1] if k + 1 < len(byte_array) else 0
            digit = byte1 + (byte2 << 8)
            block += digit << (16 * digit_index)
            digit_index += 1
        return block

    def _encrypt_string(modulus_hex: str, exponent_hex: str, plaintext: str) -> str:
        if not plaintext:
            return ""
        modulus = _hex2int(modulus_hex)
        exponent = _hex2int(exponent_hex)
        chunk_size = 2 * _bi_high_index(modulus)
        byte_array = [ord(c) for c in plaintext]
        while len(byte_array) % chunk_size != 0:
            byte_array.append(0)
        result_parts = []
        for i in range(0, len(byte_array), chunk_size):
            block = _encode_block(byte_array, i, chunk_size)
            encrypted = pow(block, exponent, modulus)
            hex_str = _int2hex(encrypted)
            result_parts.append(hex_str)
        return " ".join(result_parts)

    def _is_encrypted(password: str) -> bool:
        # Heuristic: already-encrypted passwords are hex strings.
        # A 1024-bit RSA modulus produces ~256 hex digits; even with leading
        # zeros the output is far longer than any realistic user password.
        # We keep a conservative floor (100) to avoid false positives on
        # short hex-like passwords (e.g. "deadbeef", "cafebabe").
        return (
            bool(password)
            and len(password) >= 100
            and all(c in "0123456789abcdefABCDEF" for c in password)
        )

    if _is_encrypted(password):
        return password

    modulus = "008aed7e057fe8f14c73550b0e6467b023616ddc8fa91846d2613cdb7f7621e3cada4cd5d812d627af6b87727ade4e26d26208b7326815941492b2204c3167ab2d53df1e3a2c9153bdb7c8c2e968df97a5e7e01cc410f92c4c2c2fba529b3ee988ebc1fca99ff5119e036d732c368acf8beba01aa2fdafa45b21e4de4928d0d403"
    exponent = "010001"
    encrypted = _encrypt_string(modulus, exponent, password)
    return encrypted.replace(" ", "")


def _collect_qz_cookies(session: requests.Session) -> tuple[Optional[str], Optional[int]]:
    """Extract cookie string and expiry for qz.sii.edu.cn domain."""
    cookies: dict[str, str] = {}
    expires_at: Optional[int] = None
    for c in session.cookies:
        if c.domain and (c.domain == "qz.sii.edu.cn" or c.domain.endswith(".qz.sii.edu.cn")):
            if c.value is not None:
                cookies[c.name] = c.value
            if c.name.endswith("session") and c.expires:
                expires_at = c.expires
    if not any(k.endswith("session") for k in cookies):
        return None, None
    return "; ".join(f"{k}={v}" for k, v in cookies.items()), expires_at


def _requests_cookie_to_playwright_format(
    cookie: requests.cookies.RequestsCookieJar,
) -> list[dict[str, Any]]:
    """Convert requests cookie jar entries to Playwright storage_state cookie format."""
    result: list[dict[str, Any]] = []
    for c in cookie:
        if not c.domain:
            continue
        entry: dict[str, Any] = {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
        }
        if c.expires and c.expires > 0:
            entry["expires"] = c.expires
        rest = getattr(c, "_rest", {}) or {}
        entry["httpOnly"] = bool(rest.get("HttpOnly", False))
        entry["secure"] = bool(c.secure)
        # Map requests cookie samesite to Playwright format
        same_site = rest.get("SameSite", "")
        if same_site:
            entry["sameSite"] = same_site
        result.append(entry)
    return result


def _extract_workspaces(payload: object) -> tuple[list[str], dict[str, str]]:
    """Extract workspace IDs and names from ``ListWorkspaces`` Result items."""
    if not isinstance(payload, dict):
        return [], {}

    data = payload.get("Result") or {}
    if not isinstance(data, dict):
        return [], {}

    ws_pattern = re.compile(r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    workspace_ids: list[str] = []
    workspace_names: dict[str, str] = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        workspace_id = str(item.get("id") or "").strip()
        if (
            not workspace_id
            or not ws_pattern.match(workspace_id)
            or workspace_id == DEFAULT_WORKSPACE_ID
            or workspace_id in workspace_names
        ):
            continue
        workspace_ids.append(workspace_id)
        workspace_names[workspace_id] = str(item.get("name") or "").strip()

    return workspace_ids, workspace_names


def login_with_requests(
    username: str,
    password: str,
    base_url: str = "https://api.example.com",
    headless: bool = True,
) -> WebSession:
    """Login to Inspire web UI using pure HTTP requests and capture session.

    The login flow: qz/login -> Keycloak -> CAS broker -> CAS login -> qz.
    """
    session = requests.Session()

    # Apply proxy if configured
    proxy = (os.environ.get("https_proxy") or os.environ.get("http_proxy") or "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    session.headers.update(
        {
            "User-Agent": _CAS_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )

    # Step 1: Visit base URL, follow redirects to Keycloak
    resp = session.get(base_url, timeout=30, allow_redirects=True)
    current_url = resp.url

    # Already logged in?
    parsed_url = urlparse(current_url)
    if parsed_url.netloc == "qz.sii.edu.cn":
        cookie_str, _ = _collect_qz_cookies(session)
        if cookie_str:
            # Skip CAS flow, go straight to workspace discovery
            return _build_web_session(session, username, base_url)

    # Step 2: Keycloak -> CAS broker
    if "keycloak" in current_url:
        cas_match = re.search(r'"loginUrl":\s*"([^"]*broker/cas/login[^"]*)"', resp.text)
        if not cas_match:
            raise RuntimeError("CAS login URL not found in Keycloak page")
        cas_broker_url = cas_match.group(1).replace("\\/", "/")
        if not cas_broker_url.startswith("http"):
            parsed = urlparse(current_url)
            cas_broker_url = f"{parsed.scheme}://{parsed.netloc}{cas_broker_url}"
        resp = session.get(cas_broker_url, timeout=30, allow_redirects=True)
        current_url = resp.url

    # Step 3: Must be on CAS now
    if "cas.sii.edu.cn" not in current_url:
        raise RuntimeError(f"Expected CAS login page, got: {current_url}")

    cas_login_url = current_url
    html = resp.text

    # Step 4: Extract hidden fields and submit
    encrypted_pw = _rsa_encrypt_password(password)
    login_data = {
        "username": username,
        "password": encrypted_pw,
        "_eventId": "submit",
        "loginType": "1",
        "encrypted": "true",
    }
    lt = re.search(r'name="lt"\s+value="([^"]+)"', html)
    execution = re.search(r'name="execution"\s+value="([^"]+)"', html)
    if lt:
        login_data["lt"] = lt.group(1)
    if execution:
        login_data["execution"] = execution.group(1)

    resp = session.post(
        cas_login_url,
        data=login_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://cas.sii.edu.cn",
            "Referer": cas_login_url,
            **_CAS_NAVIGATION_HEADERS,
        },
        timeout=30,
        allow_redirects=True,
    )

    # Check login result
    if "cas.sii.edu.cn" in resp.url and "login" in resp.url:
        login_error = _classify_cas_login_error(resp.text)
        if login_error:
            raise SessionAuthenticationError(login_error)
        raise SessionAuthenticationError("CAS login failed")

    # Ensure we end up on QZ
    if urlparse(resp.url).netloc != "qz.sii.edu.cn":
        resp = session.get(base_url, timeout=30, allow_redirects=True)

    cookie_str, _ = _collect_qz_cookies(session)
    if not cookie_str:
        resp = session.get(base_url, timeout=30, allow_redirects=True)
        cookie_str, _ = _collect_qz_cookies(session)
    if not cookie_str:
        raise RuntimeError("Login succeeded but no session cookie obtained")

    return _build_web_session(session, username, base_url)


def _v2_action(
    session: requests.Session,
    base_url: str,
    service: str,
    action: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST one v2 Action call on a raw requests session and return its Result.

    The platform retired /api/v1 on 2026-10-15 (#82); session verification now rides
    ``POST /api/v2/user?Action=GetUserDetail``. v2 reports failures inside
    ``ResponseMetadata.Error`` at HTTP 200, so the envelope is decoded here rather than
    trusting the status code.
    """
    resp = session.post(
        f"{base_url}/api/v2/{service}?Action={action}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"{base_url}/jobs/distributedTraining",
        },
        json=body or {},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Authentication verification failed: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError("Authentication verification failed: non-JSON response") from exc
    error = ((payload.get("ResponseMetadata") or {}).get("Error")) or {}
    if isinstance(error, dict) and error:
        raise RuntimeError(
            f"Authentication verification failed: {error.get('Code', 'Error')}: "
            f"{error.get('Message', 'unknown error')}"
        )
    result = payload.get("Result")
    return result if isinstance(result, dict) else {}


def _build_web_session(
    session: requests.Session,
    username: str,
    base_url: str,
) -> WebSession:
    """Build WebSession from authenticated requests session and live workspaces."""
    # Step 5: Verify auth and get user info (v2 Action API; /api/v1 is retired, #82).
    # The call itself is the verification: _v2_action raises unless Result comes back.
    _v2_action(session, base_url, "user", "GetUserDetail")

    # Step 6: Discover workspaces without requiring an existing workspace ID.
    workspace_id = os.environ.get("INSPIRE_WORKSPACE_ID", "").strip()
    all_workspace_ids: list[str] = []
    all_workspace_names: dict[str, str] = {}
    try:
        workspace_data = _v2_action(session, base_url, "workspace", "ListWorkspaces", {})
        all_workspace_ids, all_workspace_names = _extract_workspaces({"Result": workspace_data})
        if not workspace_id and all_workspace_ids:
            workspace_id = all_workspace_ids[0]
    except RuntimeError:
        raise
    except Exception:
        pass

    if workspace_id and workspace_id != DEFAULT_WORKSPACE_ID:
        if workspace_id not in all_workspace_ids:
            all_workspace_ids.insert(0, workspace_id)

    # Fallback: minimal session without workspace discovery
    storage_state = cast(
        "StorageState",
        {
            "cookies": _requests_cookie_to_playwright_format(session.cookies),
            "origins": [],
        },
    )
    cookies = {c.name: c.value for c in session.cookies if c.value is not None}

    web_session = WebSession(
        storage_state=storage_state,
        cookies=cookies,
        workspace_id=workspace_id or DEFAULT_WORKSPACE_ID,
        login_username=username,
        base_url=base_url,
        all_workspace_ids=all_workspace_ids or None,
        all_workspace_names=all_workspace_names or None,
        created_at=time.time(),
    )
    return web_session


def refresh_with_requests(
    session: WebSession,
    base_url: str | None = None,
) -> WebSession:
    """Refresh a cached web session through the existing SSO cookies, without a password.

    The qz console issues 'inspire-session' as a short-lived cookie and re-issues it when a
    page navigation reaches it with a valid Keycloak/CAS session.  The cached session cannot
    invoke that renewal on its own, so a later API call sees the old cookie and is redirected
    to Keycloak.  This helper performs the same page navigation, verifies the new cookie with
    the v2 Action API, and returns the refreshed state.  It raises when the SSO cookies are
    gone too; the caller then falls back to an explicit password login.
    """
    base = str(base_url or session.base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Cannot refresh a web session without a base URL")

    http = build_requests_session(session, base)
    try:
        # A stale qz cookie can make the gateway answer the page request directly.  Removing
        # it forces the SSO redirect that re-issues it; the CAS/Keycloak cookies remain.
        http.cookies.pop("inspire-session", None)
        response = http.get(base, timeout=30, allow_redirects=True)
        if urlparse(response.url).netloc != urlparse(base).netloc:
            raise SessionAuthenticationError(
                "SSO session expired; a password login is required to refresh the web session"
            )
        if response.status_code != 200:
            raise RuntimeError(f"SSO cookie refresh failed: HTTP {response.status_code}")

        _v2_action(http, base, "user", "GetUserDetail")

        original_cookies = {
            (
                str(c.get("name") or ""),
                str(c.get("domain") or ""),
                str(c.get("path") or "/"),
            ): c
            for c in (session.storage_state or {}).get("cookies") or []
            if isinstance(c, dict)
        }
        refreshed_cookies = _requests_cookie_to_playwright_format(http.cookies)
        for cookie in refreshed_cookies:
            key = (
                str(cookie.get("name") or ""),
                str(cookie.get("domain") or ""),
                str(cookie.get("path") or "/"),
            )
            original = original_cookies.get(key)
            if original is not None:
                for field in ("secure", "httpOnly"):
                    if field in original:
                        cookie[field] = bool(original[field]) or bool(cookie.get(field))
                same_site = original.get("sameSite")
                if same_site and "sameSite" not in cookie:
                    cookie["sameSite"] = same_site
            name = str(cookie.get("name") or "")
            if name.startswith("__Host-"):
                # Browser prefix rules: Secure, Path=/, and no Domain attribute.
                # requests loses these flags when its jar is rebuilt from storage_state,
                # and Playwright rejects the cookie outright when Secure is false.
                cookie["secure"] = True
                cookie["path"] = "/"
                cookie["domain"] = str(cookie.get("domain") or "").lstrip(".")
            elif name.startswith("__Secure-"):
                cookie["secure"] = True

        storage_state = cast(
            "StorageState",
            {
                "cookies": refreshed_cookies,
                "origins": list((session.storage_state or {}).get("origins") or []),
            },
        )
        cookies = {c.name: c.value for c in http.cookies if c.value is not None}
        return WebSession(
            storage_state=storage_state,
            cookies=cookies,
            workspace_id=session.workspace_id,
            account_alias=session.account_alias,
            login_username=session.login_username,
            base_url=session.base_url or base,
            all_workspace_ids=session.all_workspace_ids,
            all_workspace_names=session.all_workspace_names,
            created_at=time.time(),
        )
    finally:
        http.close()
