"""Watch mode for `inspire resources list` (façade)."""

from __future__ import annotations

from inspire.cli.commands._impl.resources_list.watch_flow import _watch_resources  # noqa: F401

__all__ = ["_watch_resources"]
