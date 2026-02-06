"""Browser (web-session) notebook APIs (Playwright rtunnel setup)."""

from __future__ import annotations

from typing import Optional

from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.core import _in_asyncio_loop, _run_in_thread
from inspire.platform.web.session import WebSession

from .flow import _setup_notebook_rtunnel_sync


def setup_notebook_rtunnel(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
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
            ssh_runtime=ssh_runtime,
            session=session,
            headless=headless,
            timeout=timeout,
        )
    return _setup_notebook_rtunnel_sync(
        notebook_id=notebook_id,
        port=port,
        ssh_port=ssh_port,
        ssh_public_key=ssh_public_key,
        ssh_runtime=ssh_runtime,
        session=session,
        headless=headless,
        timeout=timeout,
    )


__all__ = ["setup_notebook_rtunnel"]
