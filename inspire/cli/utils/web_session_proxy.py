"""Proxy helpers for Playwright-based operations."""

from __future__ import annotations

import os
from typing import Optional


def get_playwright_proxy() -> Optional[dict]:
    proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy:
        return {"server": proxy}
    return None
