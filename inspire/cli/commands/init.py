"""Init command façade for Inspire CLI.

The implementation lives in `init_helpers.py`. This file re-exports the public API so existing
imports (and tests) keep working.
"""

from __future__ import annotations

from inspire.cli.commands.init_helpers import (  # noqa: F401
    CONFIG_TEMPLATE,
    _detect_env_vars,
    _generate_toml_content,
    init,
)

__all__ = [
    "CONFIG_TEMPLATE",
    "_detect_env_vars",
    "_generate_toml_content",
    "init",
]
