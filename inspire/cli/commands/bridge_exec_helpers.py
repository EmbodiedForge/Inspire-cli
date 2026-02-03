"""Helpers for `inspire bridge exec` (façade).

Kept separate so `inspire/cli/commands/bridge.py` can stay small and focused on Click wiring.

Important: The Click command module (`bridge.py`) is monkeypatched by tests, so these helpers
accept dependency callables (tunnel + workflow) as parameters instead of importing them directly.
This keeps tests able to patch `inspire.cli.commands.bridge.<name>` as before.
"""

from __future__ import annotations

from inspire.cli.commands.bridge_exec_helpers_ssh import (  # noqa: F401
    try_exec_via_ssh_tunnel,
)
from inspire.cli.commands.bridge_exec_helpers_workflow import (  # noqa: F401
    exec_via_workflow,
    split_denylist,
)

__all__ = [
    "exec_via_workflow",
    "split_denylist",
    "try_exec_via_ssh_tunnel",
]
