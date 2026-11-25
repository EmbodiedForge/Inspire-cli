"""Priority constraints attached to workspace scheduling offers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PRIORITY_LEVEL_BANDS: dict[str, tuple[int, int]] = {
    "low": (1, 3),
    "normal": (4, 6),
    "medium": (4, 6),
    "high": (7, 10),
}

# A request inside the LOW band is itself preemptible, so capacity held by other
# low-priority jobs is not free space for it - it is the very budget it would be
# preempted out of.  Only a request at or above the band ceiling plus one may take
# that capacity over.
PREEMPT_MIN_PRIORITY = PRIORITY_LEVEL_BANDS["low"][1] + 1


def normalize_priority_levels(value: Any) -> tuple[str, ...]:
    """Normalize a schedule's priority-level declaration."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else []
        except json.JSONDecodeError:
            value = []
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(
        level
        for level in (str(item or "").strip().lower() for item in value)
        if level in PRIORITY_LEVEL_BANDS
    )


def priority_can_preempt(priority: int | None) -> bool:
    """Return whether a request at ``priority`` may count preemptible capacity.

    ``None`` means the priority is not decided yet, which keeps the previous
    behaviour of treating low-priority capacity as usable.
    """
    if priority is None:
        return True
    return int(priority) >= PREEMPT_MIN_PRIORITY


def priority_matches_allowed(priority: int | None, allowed_levels: Any) -> bool:
    """Return whether a numeric task priority is accepted by an offer."""
    if priority is None:
        return True
    levels = normalize_priority_levels(allowed_levels)
    if not levels:
        return True
    return any(
        lower <= priority <= upper
        for level in levels
        for lower, upper in (PRIORITY_LEVEL_BANDS[level],)
    )


def highest_declared_priority(levels: Any) -> int | None:
    """The highest ``task_priority`` a set of declared priority levels takes.

    Each level names a band of numbers, so the ceiling is the highest upper bound among the
    levels: ``low`` alone gives 3, ``normal`` gives 6, ``low`` together with ``normal`` gives 6.
    Nothing declared gives ``None`` rather than a number nobody declared — an empty declaration
    is unknown, never an open range.
    """
    declared = normalize_priority_levels(levels)
    if not declared:
        return None
    return max(PRIORITY_LEVEL_BANDS[level][1] for level in declared)


@dataclass(frozen=True)
class OfferPriorityEvidence:
    """Priority declarations that apply to one offer, or why none can be inferred."""

    status: str
    scope: str
    levels: tuple[str, ...] = ()
    declaration_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ScheduleRow:
    source: str
    quota_id: str
    group_ids: tuple[str, ...]
    levels: tuple[str, ...]
    shape: tuple[int, int, int] | None


def _schedule_rows(schedule: dict[str, Any]) -> list[_ScheduleRow]:
    rows: list[_ScheduleRow] = []
    for key in ("quota", "predef_train_spec"):
        raw_rows = schedule.get(key, [])
        if isinstance(raw_rows, str):
            try:
                raw_rows = json.loads(raw_rows) if raw_rows else []
            except json.JSONDecodeError:
                raw_rows = []
        if not isinstance(raw_rows, list):
            continue
        for index, row in enumerate(raw_rows):
            if not isinstance(row, dict):
                continue
            quota_id = str(row.get("id") or row.get("quota_id") or "").strip()
            raw_group_ids = row.get("logic_compute_group_ids") or []
            if isinstance(raw_group_ids, str):
                raw_group_ids = [raw_group_ids]
            if not isinstance(raw_group_ids, list):
                raw_group_ids = []
            group_ids = tuple(str(group_id or "").strip() for group_id in raw_group_ids if group_id)
            rows.append(
                _ScheduleRow(
                    source=str(quota_id or f"{key}[{index}]"),
                    quota_id=quota_id,
                    group_ids=group_ids or ("*",),
                    levels=normalize_priority_levels(row.get("allowed_priority_levels")),
                    shape=_offer_shape(row),
                )
            )
    return rows


def _offer_shape(row: dict[str, Any]) -> tuple[int, int, int] | None:
    candidates = [row]
    for key in ("resource_spec_price", "resource_spec", "spec"):
        nested = row.get(key)
        if isinstance(nested, dict):
            candidates.insert(0, nested)
    for candidate in candidates:
        values = []
        for keys in (
            ("gpu_count", "gpu"),
            ("cpu_count", "cpu"),
            ("memory_size_gib", "memory_gib", "mem_gi", "memory_size"),
        ):
            value: Any = next(
                (candidate[key] for key in keys if candidate.get(key) is not None), None
            )
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                break
        if len(values) == 3:
            return values[0], values[1], values[2]
    return None


def schedule_priority_levels_by_offer(
    schedule: dict[str, Any],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Index exact declarations for compatibility; use evidence resolution for launches."""
    indexed: dict[tuple[str, str], tuple[str, ...]] = {}
    for row in _schedule_rows(schedule):
        if not row.quota_id or not row.levels:
            continue
        for group_id in row.group_ids:
            indexed.setdefault((group_id, row.quota_id), row.levels)
    return indexed


def _evidence(rows: list[_ScheduleRow], scope: str) -> OfferPriorityEvidence:
    if not rows:
        return OfferPriorityEvidence(status="unknown", scope="none")
    level_sets = {tuple(sorted(set(row.levels))) for row in rows}
    ids = tuple(sorted({row.source for row in rows}))
    if len(level_sets) > 1:
        return OfferPriorityEvidence(status="conflict", scope=scope, declaration_ids=ids)
    levels = next(iter(level_sets))
    if not levels:
        return OfferPriorityEvidence(status="unknown", scope=scope, declaration_ids=ids)
    return OfferPriorityEvidence(status="declared", scope=scope, levels=levels, declaration_ids=ids)


def priority_evidence_for_offer(
    schedule: dict[str, Any],
    *,
    group_id: str,
    quota_id: str,
    gpu_count: int | None = None,
    cpu_count: int | None = None,
    memory_size_gib: int | None = None,
) -> OfferPriorityEvidence:
    """Resolve declarations without borrowing another group or resource shape's limits."""
    group = str(group_id or "").strip()
    quota = str(quota_id or "").strip()
    rows = _schedule_rows(schedule)
    exact = [row for row in rows if group in row.group_ids and row.quota_id == quota]
    if exact:
        return _evidence(exact, "group_quota")
    generic = [row for row in rows if "*" in row.group_ids and row.quota_id == quota]
    if generic:
        return _evidence(generic, "quota")

    group_rows = [row for row in rows if group in row.group_ids]
    shape = (
        (int(gpu_count), int(cpu_count), int(memory_size_gib))
        if gpu_count is not None and cpu_count is not None and memory_size_gib is not None
        else None
    )
    if shape is not None:
        matching_shape = [row for row in group_rows if row.shape == shape]
        if matching_shape:
            return _evidence(matching_shape, "group_shape")

    nonempty = [row for row in group_rows if row.levels]
    if group_rows and len(nonempty) == len(group_rows):
        evidence = _evidence(nonempty, "group")
        if evidence.status == "conflict":
            return evidence
        return OfferPriorityEvidence(
            status="declared",
            scope="group",
            levels=evidence.levels,
            declaration_ids=evidence.declaration_ids,
        )
    if len({tuple(sorted(set(row.levels))) for row in nonempty}) > 1:
        return _evidence(nonempty, "group")
    return OfferPriorityEvidence(status="unknown", scope="none")


def allowed_priority_levels_for_offer(
    schedule: dict[str, Any],
    *,
    group_id: str,
    quota_id: str,
) -> tuple[str, ...]:
    """Return unambiguous exact priority levels for one group/quota offer."""
    evidence = priority_evidence_for_offer(schedule, group_id=group_id, quota_id=quota_id)
    return evidence.levels if evidence.status == "declared" else ()


def workspace_declared_priority_levels(schedule: dict[str, Any]) -> tuple[str, ...]:
    """Return schedule-level diagnostics; never use this union as a launch ceiling."""
    levels: set[str] = set()
    for row in _schedule_rows(schedule):
        levels.update(row.levels)
    return tuple(sorted(levels))


__all__ = [
    "PRIORITY_LEVEL_BANDS",
    "PREEMPT_MIN_PRIORITY",
    "OfferPriorityEvidence",
    "allowed_priority_levels_for_offer",
    "highest_declared_priority",
    "normalize_priority_levels",
    "priority_can_preempt",
    "priority_evidence_for_offer",
    "priority_matches_allowed",
    "schedule_priority_levels_by_offer",
    "workspace_declared_priority_levels",
]
