"""Session cache for the SII data-square portal (aip.sii.edu.cn).

The data square is a second, separate web application: it authenticates with a single
``datasets-session`` JWT cookie that its own login page issues, on a different host from the
compute platform, and it has no token endpoint at all. So the credential here is captured the
same way the compute-platform web session is - a browser hands over its storage state - and it
is stored in its own cache file, next to but never mixed with ``web_session*.json``.

Cookie isolation comes free: ``build_requests_session`` seeds a jar from the session's own
storage state and requests only sends cookies whose domain matches the request host, so a
data-square request can never carry a compute-platform cookie, or the other way round.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from inspire.config.toml_write import atomic_write_text
from inspire.platform.web.session import models as session_models
from inspire.platform.web.session.models import WebSession, normalize_account_for_cache

DATASQUARE_BASE_URL_ENV = "INSPIRE_DATASQUARE_BASE_URL"
DEFAULT_DATASQUARE_BASE_URL = "https://aip.sii.edu.cn"

# The ``datasets-session`` JWT is issued with a 24-hour lifetime.
DATASQUARE_SESSION_TTL = 24 * 3600


def get_datasquare_base_url() -> str:
    """Resolve the data-square base URL, overridable for testing against a stub."""
    override = os.environ.get(DATASQUARE_BASE_URL_ENV, "").strip()
    return (override or DEFAULT_DATASQUARE_BASE_URL).rstrip("/")


def get_datasquare_session_cache_file(account: Optional[str] = None) -> Path:
    """The cache file for one account's data-square session.

    Read off ``session_models.SESSION_CACHE_DIR`` at call time rather than bound at import, so
    a test that redirects the shared cache directory also redirects this one.
    """
    normalized = normalize_account_for_cache(account)
    if not normalized:
        return session_models.SESSION_CACHE_DIR / "datasquare_session.json"
    return session_models.SESSION_CACHE_DIR / f"datasquare_session-{normalized}.json"


def is_datasquare_session_valid(session: WebSession) -> bool:
    """Whether a cached data-square session is still inside its own TTL.

    ``WebSession.is_valid`` answers for the compute platform's one-hour cache; reusing it here
    would throw away 23 valid hours of the data-square JWT.
    """
    return (time.time() - session.created_at) < DATASQUARE_SESSION_TTL


def _default_account_cache_key() -> Optional[str]:
    """The account the runtime configuration points at.

    Mirrors how the compute-platform session resolves its cache key, so
    ``inspire dataset list`` without ``--account`` reads the same account's cache file as
    every other command instead of an unscoped file nothing writes. Any config problem
    (no config yet, no account selected) degrades to the unscoped file.
    """
    try:
        from inspire.config import Config

        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        return config.get_account_context().cache_key or None
    except Exception:
        return None


def get_datasquare_session(
    account: Optional[str] = None,
    allow_expired: bool = False,
) -> Optional[WebSession]:
    """Load a cached data-square session, or None when there is nothing usable on disk.

    There is no re-login path to fall back on - the portal has no token endpoint - so a miss is
    reported to the caller rather than silently launching a browser.
    """
    cache_file = get_datasquare_session_cache_file(account or _default_account_cache_key())
    try:
        with open(cache_file) as handle:
            data = json.load(handle)
        session = WebSession.from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    if not session.storage_state.get("cookies") and not session.cookies:
        return None
    if allow_expired or is_datasquare_session_valid(session):
        return session
    return None


def save_datasquare_session(session: WebSession, account: Optional[str] = None) -> Path:
    """Persist a data-square session and return the file it was written to."""
    cache_file = get_datasquare_session_cache_file(
        account or session.account_alias or session.login_username
    )
    atomic_write_text(cache_file, json.dumps(session.to_dict()))
    return cache_file


__all__ = [
    "DATASQUARE_BASE_URL_ENV",
    "DATASQUARE_SESSION_TTL",
    "DEFAULT_DATASQUARE_BASE_URL",
    "get_datasquare_base_url",
    "get_datasquare_session",
    "get_datasquare_session_cache_file",
    "is_datasquare_session_valid",
    "save_datasquare_session",
]
