"""Browser (web-session) notebook APIs (Playwright rtunnel setup).

This module keeps the historical import path stable while splitting the implementation into
smaller internal modules under `inspire.cli.utils._impl.browser_api.notebooks.playwright.rtunnel`.
"""

from __future__ import annotations

from typing import Optional

from inspire.cli.utils.browser_api_core import (
    _in_asyncio_loop,
    _run_in_thread,
)
from inspire.cli.utils._impl.browser_api.notebooks.playwright.rtunnel.flow import (
    _setup_notebook_rtunnel_sync,
)
from inspire.cli.utils.web_session import WebSession


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
