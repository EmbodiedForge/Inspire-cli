"""Factory for creating the appropriate terminal transport."""

from __future__ import annotations

from typing import Any

from inspire.platform.web.session import WebSession

from .transports import PlaywrightDomTransport, PythonWsTransport, TerminalTransport


def create_transport(
    *,
    auth_method: str,
    session: WebSession,
    lab_url: str,
    token: str | None = None,
    context: Any = None,
    page: Any = None,
    lab_frame: Any = None,
) -> TerminalTransport:
    """Create the appropriate transport based on auth method.

    Args:
        auth_method: ``"http"`` for Python-native WebSocket transport,
            ``"playwright"`` for DOM-based Playwright transport.
        session: Active web session (used for HTTP transport).
        lab_url: Resolved JupyterLab URL.
        token: Optional Jupyter authentication token (used for HTTP transport).
        context: Playwright browser context (only for playwright method).
        page: Playwright page (only for playwright method).
        lab_frame: Playwright lab frame (only for playwright method).

    Returns:
        A ``TerminalTransport`` implementation.
    """
    if auth_method == "http":
        return PythonWsTransport(session, lab_url, token=token)
    return PlaywrightDomTransport(context, page, lab_frame)
