"""Forge helper functions for workflow listing/matching."""

from __future__ import annotations

import json
from typing import Optional


def _extract_total_count(response: dict) -> Optional[int]:
    """Extract total count from a workflow runs response."""
    total_count = response.get("total_count") or response.get("total") or response.get("count")
    try:
        return int(total_count) if total_count is not None else None
    except (TypeError, ValueError):
        return None


def _parse_event_inputs(run: dict) -> dict:
    """Parse inputs from a workflow run's event payload."""
    event_payload = run.get("event_payload", "")
    if not event_payload:
        return {}
    try:
        payload = json.loads(event_payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    inputs = payload.get("inputs", {}) or {}
    return inputs if isinstance(inputs, dict) else {}


def _matches_inputs(inputs: dict, expected_inputs: dict) -> bool:
    """Check if inputs match expected values."""
    for key, value in expected_inputs.items():
        if not value:
            continue
        if str(inputs.get(key, "")) != str(value):
            return False
    return True


def _find_run_by_inputs(runs: list, expected_inputs: dict) -> Optional[dict]:
    """Find a workflow run matching the expected inputs."""
    for run in runs:
        inputs = _parse_event_inputs(run)
        if not inputs:
            continue
        if _matches_inputs(inputs, expected_inputs):
            return run
    return None


def _artifact_name(job_id: str, request_id: str) -> str:
    """Compute the artifact name from job_id and request_id."""
    return f"job-{job_id}-log-{request_id}"
