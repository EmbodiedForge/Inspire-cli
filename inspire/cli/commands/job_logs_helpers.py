"""Helpers for the `inspire job logs` command (façade).

The implementation is split into smaller modules; this file re-exports the internal helpers used
by `job_logs_flow.py`.
"""

from __future__ import annotations

from inspire.cli.commands.job_logs_bulk import _bulk_update_logs  # noqa: F401
from inspire.cli.commands.job_logs_follow import _follow_logs  # noqa: F401
from inspire.cli.commands.job_logs_ssh import (  # noqa: F401
    _fetch_log_via_ssh,
    _follow_logs_via_ssh,
)

__all__ = [
    "_bulk_update_logs",
    "_fetch_log_via_ssh",
    "_follow_logs",
    "_follow_logs_via_ssh",
]


pass
