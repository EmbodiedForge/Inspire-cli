"""Session values that must not leak into command output."""

from __future__ import annotations

from typing import Any


def known_secret_values(session: Any) -> tuple[str, ...]:
    """Return cookie values loaded into a browser, without exposing them in reports."""
    values: list[str] = []
    storage = getattr(session, "storage_state", None)
    cookies = storage.get("cookies") if isinstance(storage, dict) else None
    if isinstance(cookies, list):
        for cookie in cookies:
            if isinstance(cookie, dict):
                value = str(cookie.get("value") or "")
                if len(value) >= 8:
                    values.append(value)
    legacy_cookies = getattr(session, "cookies", None)
    if isinstance(legacy_cookies, dict):
        values.extend(str(value) for value in legacy_cookies.values() if len(str(value)) >= 8)
    return tuple(dict.fromkeys(values))


__all__ = ["known_secret_values"]
