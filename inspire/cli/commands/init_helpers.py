"""Init command façade for Inspire CLI.

The implementation is split across smaller modules. This file re-exports the public API to keep
imports stable.
"""

from __future__ import annotations

from inspire.cli.commands.init_env import _detect_env_vars, _generate_toml_content  # noqa: F401
from inspire.cli.commands.init_flow import init  # noqa: F401
from inspire.cli.commands.init_template import CONFIG_TEMPLATE  # noqa: F401

__all__ = [
    "CONFIG_TEMPLATE",
    "_detect_env_vars",
    "_generate_toml_content",
    "init",
]
