"""Agent-facing scheduling notes shared by job and notebook status."""

from __future__ import annotations

from typing import Any

_PENDING_MARKERS = ("queu", "pending", "creating", "starting", "restart")
_QUOTA_RESOURCES = (("gpu", "GPU"), ("cpu", "CPU"), ("memory_gi", "memory GiB"))


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _limit(value: Any) -> int | None:
    """Return the platform limit, or None when it is absent or explicitly unlimited (-1)."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return None if number == -1 else number


def project_quota_pending_hint(quota: object, project_name: object = None) -> str | None:
    """Explain a PENDING job/notebook when GetProjectQuotaJobs reports no room left.

    The platform exposes the project quota block (used/limit for GPU, CPU and memory); a limit of
    -1 means unlimited, while 0 or used >= limit means new work cannot be admitted.  This is the
    platform's own "project full" evidence and outranks the generic parallel-slot note.
    """
    if not isinstance(quota, dict):
        return None
    exhausted = []
    for key, label in _QUOTA_RESOURCES:
        used = _as_int(quota.get(f"{key}_used"))
        limit = _limit(quota.get(f"{key}_limit"))
        if limit is not None and limit >= 0 and used >= limit:
            exhausted.append(f"{label} {used} used / {limit} limit")
    if not exhausted:
        return None
    details = ", ".join(exhausted)
    project = str(project_name or "").strip()
    where = f"the '{project}' project" if project else "this project"
    return (
        f"Project quota is exhausted: {where} reports {details}; new work queues. "
        "Release/stop work, or retry with `--project <another authorized project>`."
    )


def pending_project_slot_hint(status: object, project_name: object = None) -> str | None:
    """Explain a PENDING workload when no project-quota limit explains it.

    The platform exposes no task-count/parallel-slot field through the project APIs, so this is
    deliberately a pointer to the evidence (active jobs/notebooks), not a claim that a slot is full.
    """
    text = str(status or "").strip().lower()
    if not text or not any(marker in text for marker in _PENDING_MARKERS):
        return None
    project = str(project_name or "").strip()
    where = f"the '{project}' project" if project else "this project"
    return (
        f"No scheduler objection is reported. If the pool shows free GPUs, {where} may be out "
        "of parallel-workload slots; compare `inspire job list --active` and "
        "`inspire notebook list`, or retry with `--project <another authorized project>`."
    )


__all__ = ["pending_project_slot_hint", "project_quota_pending_hint"]
