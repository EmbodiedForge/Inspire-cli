"""Proxy helpers for Playwright-based operations."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from playwright.sync_api import ProxySettings


def get_playwright_proxy() -> Optional[ProxySettings]:
    proxy = (os.environ.get("https_proxy") or os.environ.get("http_proxy") or "").strip()
    if proxy:
        return {"server": proxy}
    return None
