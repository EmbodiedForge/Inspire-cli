"""Notebook creation flow (façade).

The implementation is split into smaller modules; this file re-exports the public API
to keep import paths stable.
"""

from __future__ import annotations

from inspire.cli.commands._impl.notebook_create.run import run_notebook_create  # noqa: F401

__all__ = ["run_notebook_create"]
