"""Browser (web-session) notebook APIs (Playwright flows).

Historically notebook-related Playwright flows lived in one large module. The implementation is
now split into smaller modules; this file re-exports the public API to keep import paths stable.
"""

from __future__ import annotations

from inspire.cli.utils.browser_api_notebooks_playwright_exec import run_command_in_notebook
from inspire.cli.utils.browser_api_notebooks_playwright_rtunnel import setup_notebook_rtunnel

__all__ = [
    "run_command_in_notebook",
    "setup_notebook_rtunnel",
]
