"""Playwright browser runtime detection and messaging.

The `playwright` Python package and the browser binary it drives are installed
separately. A missing binary raises a deep Playwright traceback whose only
actionable line is buried in an ASCII box, which users read as "the CLI crashed".
Detect it once here so every browser path can say the same short thing instead.
"""

from __future__ import annotations

__all__ = [
    "MISSING_BROWSER_HINT",
    "PLAYWRIGHT_INSTALL_HINT",
    "MissingBrowserError",
    "is_missing_browser_error",
    "missing_browser_error",
]

PLAYWRIGHT_INSTALL_HINT = (
    "Playwright is not installed; this browser command needs the optional browser extra.\n"
    "  Installed as a tool:\n"
    "    uv tool install --force --refresh "
    "'inspire-cli[browser] @ git+https://codeberg.org/cyteena/inspire-cli.git'\n"
    '    "$(uv tool dir)/inspire-cli/bin/playwright" install chromium\n'
    "  In a checkout:       uv sync --extra browser && uv run playwright install chromium"
)

MISSING_BROWSER_HINT = (
    "  In a checkout:       uv sync --extra browser && uv run playwright install chromium\n"
    '  Installed as a tool: "$(uv tool dir)/inspire-cli/bin/playwright" install chromium'
)


class MissingBrowserError(RuntimeError):
    """Playwright is importable but its Chromium binary is not on disk."""


def is_missing_browser_error(exc: BaseException) -> bool:
    """True when *exc* is Playwright complaining about its own browser binary."""
    text = str(exc)
    return "Executable doesn't exist" in text or "Looks like Playwright was just installed" in text


def missing_browser_error(purpose: str) -> MissingBrowserError:
    """Build the error to raise, stating what the browser was needed for.

    The caller has already decided the binary is missing, so ``purpose`` is the
    one truthful line of context that tells the user why this blocks them.
    """
    return MissingBrowserError(
        f"Playwright browser is not installed; {purpose}.\n{MISSING_BROWSER_HINT}"
    )
