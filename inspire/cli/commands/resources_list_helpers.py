"""`inspire resources list` helpers façade.

The implementation is split into smaller modules; this file re-exports the public API so
`resources_list.py` stays stable.
"""

from __future__ import annotations

from inspire.cli.commands.resources_list_flow import run_resources_list  # noqa: F401

__all__ = ["run_resources_list"]


pass
