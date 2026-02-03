"""Shared helpers for job commands."""

from __future__ import annotations

from inspire.api import _validate_job_id_format
from inspire.cli.context import Context, EXIT_JOB_NOT_FOUND
from inspire.cli.utils.errors import exit_with_error as _handle_error


def _ensure_valid_job_id(ctx: Context, job_id: str) -> bool:
    format_error = _validate_job_id_format(job_id)
    if format_error:
        _handle_error(ctx, "InvalidJobID", format_error, EXIT_JOB_NOT_FOUND)
        return False
    return True
