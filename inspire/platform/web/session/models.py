"""Web session models and cache persistence."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

from inspire.config.toml_write import atomic_write_text

if TYPE_CHECKING:
    from playwright.sync_api import StorageState

# Session cache files
SESSION_CACHE_DIR = Path.home() / ".cache" / "inspire-cli"
SESSION_CACHE_FILE = SESSION_CACHE_DIR / "web_session.json"  # legacy/default path
SESSION_TTL = 3600  # 1 hour


class SessionExpiredError(Exception):
    """Raised when the web session has expired (401 from server)."""


class SessionAuthenticationError(RuntimeError):
    """Raised when CAS rejects an explicit authentication attempt."""


class SessionRefreshError(SessionExpiredError):
    """Raised when one session's single re-authentication attempt fails."""


# Default workspace placeholder (override with INSPIRE_WORKSPACE_ID env var)
DEFAULT_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"


def normalize_account_for_cache(account: Optional[str]) -> Optional[str]:
    if not account:
        return None
    value = account.strip()
    if not value:
        return None
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return normalized or None


def get_session_cache_file(account: Optional[str] = None) -> Path:
    normalized = normalize_account_for_cache(account)
    if not normalized:
        return SESSION_CACHE_FILE
    return SESSION_CACHE_DIR / f"web_session-{normalized}.json"


def get_auth_failure_cache_file(account: Optional[str] = None) -> Path:
    """Return the account-scoped cross-process authentication cooldown file."""
    normalized = normalize_account_for_cache(account) or "default"
    return SESSION_CACHE_DIR / f"web_auth_failure-{normalized}.json"


@contextmanager
def _lock_web_session_cache(path: Path) -> Iterator[None]:
    if os.name != "posix":
        yield
        return

    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@dataclass
class WebSession:
    """Captured web session for web-ui APIs.

    We store Playwright `storage_state` because the web-ui APIs behind `/api/*`
    are protected by Keycloak/CAS SSO and can require more than just a couple
    of cookies.
    """

    storage_state: StorageState
    created_at: float
    workspace_id: Optional[str] = None
    account_alias: Optional[str] = None
    login_username: Optional[str] = None
    base_url: Optional[str] = None
    all_workspace_ids: Optional[list[str]] = None
    all_workspace_names: Optional[dict[str, str]] = None

    # Back-compat: older cache stored only name->value cookies
    cookies: Optional[dict[str, str]] = None
    _refresh_attempted: bool = field(default=False, init=False, repr=False, compare=False)
    _refresh_failure_message: Optional[str] = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def is_valid(self) -> bool:
        """Check if session is still valid (not expired)."""
        return (time.time() - self.created_at) < SESSION_TTL

    def to_dict(self) -> dict:
        return {
            "storage_state": self.storage_state,
            "cookies": self.cookies,
            "workspace_id": self.workspace_id,
            "account_alias": self.account_alias,
            "login_username": self.login_username,
            "base_url": self.base_url,
            "all_workspace_ids": self.all_workspace_ids,
            "all_workspace_names": self.all_workspace_names,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WebSession":
        # Back-compat with older cache files that stored only cookies
        storage_state = cast("StorageState | None", data.get("storage_state"))
        cookies = data.get("cookies")
        if storage_state is None:
            storage_state = {"cookies": [], "origins": []}
        return cls(
            storage_state=storage_state,
            cookies=cookies,
            workspace_id=data.get("workspace_id"),
            account_alias=data.get("account_alias"),
            login_username=data.get("login_username"),
            base_url=data.get("base_url"),
            all_workspace_ids=data.get("all_workspace_ids"),
            all_workspace_names=data.get("all_workspace_names"),
            created_at=data["created_at"],
        )

    def save(self, account: Optional[str] = None) -> None:
        """Save session to cache file."""
        cache_file = get_session_cache_file(account or self.account_alias or self.login_username)
        with _lock_web_session_cache(cache_file):
            atomic_write_text(cache_file, json.dumps(self.to_dict()))

    def save_if_current(self, previous: WebSession, *, account: str) -> bool:
        """Persist a refresh only if the original scoped cache is still current."""
        cache_file = get_session_cache_file(account)
        with _lock_web_session_cache(cache_file):
            try:
                cached = WebSession.from_dict(json.loads(cache_file.read_text(encoding="utf-8")))
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                return False
            if cached.to_dict() != previous.to_dict():
                return False
            atomic_write_text(cache_file, json.dumps(self.to_dict()))
            return True

    @classmethod
    def load(
        cls,
        allow_expired: bool = False,
        account: Optional[str] = None,
    ) -> Optional["WebSession"]:
        """Load session from cache file if valid.

        Args:
            allow_expired: If True, return session even if TTL has expired.
                          The session cookies may still be valid server-side.
        """
        candidates: list[Path] = []
        scoped_file = get_session_cache_file(account)
        if scoped_file.exists():
            candidates.append(scoped_file)
        # Backward compatibility: if account-scoped file is missing or invalid,
        # fall back to the legacy shared cache path.
        if scoped_file != SESSION_CACHE_FILE and SESSION_CACHE_FILE.exists():
            candidates.append(SESSION_CACHE_FILE)
        elif not candidates and SESSION_CACHE_FILE.exists():
            candidates.append(SESSION_CACHE_FILE)

        for cache_file in candidates:
            try:
                with open(cache_file) as f:
                    data = json.load(f)
                session = cls.from_dict(data)
                if allow_expired or session.is_valid():
                    return session
            except (json.JSONDecodeError, KeyError):
                continue
        return None
