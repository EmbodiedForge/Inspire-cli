"""Centralized dependencies for job-related CLI code.

The `inspire job ...` commands historically used a custom "deps" injection pattern to make
unit tests easy to monkeypatch. This module keeps that convenience while allowing job
subcommands to be defined as normal Click commands (no command factories).

Tests can patch attributes on this module (e.g., `time.time`) and those patches will be
observed across all job command modules.
"""

from __future__ import annotations

import time

__all__ = [
    "time",
]
