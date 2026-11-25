"""Workspace scheduling policies for interactive and batch workloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.session import WebSession

from .core import _get_base_url, _request_json
from .errors import get_browser_api_action_result

_CRITERIA = {
    "GPU": "gpu",
    "CPU": "cpu",
    "MEM": "memory",
    "RUNTIME": "runtime",
}


@dataclass(frozen=True)
class ReclaimCondition:
    """One condition in a workspace reclaim rule."""

    criterion: str
    hours: float
    threshold: Optional[float] = None

    def describe(self) -> str:
        if self.criterion == "runtime":
            return f"runtime > {_hours(self.hours)}"
        if self.threshold is None:
            return f"{self.criterion} idle for {_hours(self.hours)}"
        return f"{self.criterion} < {_percent(self.threshold)} for {_hours(self.hours)}"


@dataclass(frozen=True)
class ReclaimRule:
    """A reclaim rule with the platform's nested boolean gates preserved."""

    gate: str
    groups: tuple[tuple[str, tuple[ReclaimCondition, ...]], ...]

    def describe(self) -> str:
        parts: list[str] = []
        for gate, conditions in self.groups:
            if not conditions:
                continue
            text = f" {gate.upper()} ".join(condition.describe() for condition in conditions)
            if len(conditions) > 1 and len(self.groups) > 1:
                text = f"({text})"
            parts.append(text)
        return f" {self.gate.upper()} ".join(parts)


@dataclass(frozen=True)
class WorkloadSchedulePolicy:
    """The declared reclaim and runtime policy for one workload."""

    workload: str
    configured: bool = True
    auto_reclaim: Optional[bool] = None
    reclaim_rule: Optional[ReclaimRule] = None
    max_runtime_minutes: Optional[int] = None
    daily_shutdown: Optional[str] = None
    auto_save: Optional[bool] = None
    applies_to: Optional[str] = None

    @property
    def reclaim_description(self) -> Optional[str]:
        if self.reclaim_rule is None:
            return None
        described = self.reclaim_rule.describe()
        if not described:
            return None
        if self.applies_to:
            return f"{described} ({self.applies_to})"
        return described


def _hours(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value))}h"
    return f"{value:g}h"


def _percent(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value))}%"
    return f"{value:g}%"


def format_duration(minutes: Optional[int]) -> Optional[str]:
    """Render a positive minute count as days, hours, and minutes."""
    if not minutes or minutes <= 0:
        return None
    days, remainder = divmod(int(minutes), 24 * 60)
    hours, mins = divmod(remainder, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m" if mins else ""]
    return " ".join(part for part in parts if part)


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _gate(value: Any) -> str:
    return "and" if str(value or "").strip().upper() == "AND" else "or"


def _parse_rule(raw: Any) -> Optional[ReclaimRule]:
    payload = raw
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
    if not isinstance(payload, dict) or not payload:
        return None

    groups: list[tuple[str, tuple[ReclaimCondition, ...]]] = []
    for group in payload.get("conds") or []:
        if not isinstance(group, dict):
            continue
        conditions: list[ReclaimCondition] = []
        for condition in group.get("conds") or []:
            if not isinstance(condition, dict):
                continue
            criterion = _CRITERIA.get(str(condition.get("crit") or "").strip().upper())
            if not criterion:
                continue
            conditions.append(
                ReclaimCondition(
                    criterion=criterion,
                    hours=_number(condition.get("hrs")),
                    threshold=(
                        None if criterion == "runtime" else _number(condition.get("thresh"))
                    ),
                )
            )
        if conditions:
            groups.append((_gate(group.get("gate")), tuple(conditions)))
    if not groups:
        return None
    return ReclaimRule(gate=_gate(payload.get("gate")), groups=tuple(groups))


def _notebook_rule(shared: dict[str, Any]) -> Optional[ReclaimRule]:
    rule = _parse_rule(shared.get("recycle_config"))
    if rule is not None:
        return rule

    hours = _number(shared.get("recycle_hour"))
    criterion = _CRITERIA.get(str(shared.get("recycle_standard") or "").strip().upper())
    threshold = _number(shared.get("recycle_rate"))
    if not hours or not criterion or not threshold:
        return None
    condition = ReclaimCondition(
        criterion=criterion,
        hours=hours,
        threshold=None if criterion == "runtime" else threshold,
    )
    return ReclaimRule(gate="or", groups=(("or", (condition,)),))


def _timed_minutes(payload: dict[str, Any], day: str, hour: str, minute: str) -> Optional[int]:
    minutes = (
        _int(payload.get(day)) * 24 * 60 + _int(payload.get(hour)) * 60 + _int(payload.get(minute))
    )
    return minutes or None


def _clock(hour: Any, minute: Any) -> str:
    return f"{_int(hour):02d}:{_int(minute):02d}"


def _notebook_policy(shared: dict[str, Any]) -> WorkloadSchedulePolicy:
    return WorkloadSchedulePolicy(
        workload="notebook",
        auto_reclaim=_flag(shared.get("auto_recycle")),
        reclaim_rule=_notebook_rule(shared),
        daily_shutdown=(
            _clock(shared.get("shutdown_hour"), shared.get("shutdown_minute"))
            if _flag(shared.get("timed_shutdown"))
            else None
        ),
        auto_save=_flag(shared.get("shutdown_save")) or _flag(shared.get("recycle_save")),
    )


def _train_policy(shared: dict[str, Any]) -> WorkloadSchedulePolicy:
    return WorkloadSchedulePolicy(
        workload="job",
        auto_reclaim=_flag(shared.get("auto_recycle_train")),
        reclaim_rule=_parse_rule(shared.get("auto_recycle_train_ruleset")),
        max_runtime_minutes=(
            _timed_minutes(
                shared,
                "recycle_train_day",
                "recycle_train_hour",
                "recycle_train_minute",
            )
            if _flag(shared.get("timed_recycle_train"))
            else None
        ),
    )


def _ray_policy(shared: dict[str, Any]) -> WorkloadSchedulePolicy:
    return WorkloadSchedulePolicy(
        workload="ray",
        auto_reclaim=_flag(shared.get("auto_recycle_rayjob")),
        reclaim_rule=_parse_rule(shared.get("auto_recycle_rayjob_ruleset")),
        max_runtime_minutes=(
            _timed_minutes(
                shared,
                "recycle_rayjob_day",
                "recycle_rayjob_hour",
                "recycle_rayjob_minute",
            )
            if _flag(shared.get("timed_recycle_rayjob"))
            else None
        ),
    )


def _hpc_policy(payload: dict[str, Any]) -> WorkloadSchedulePolicy:
    if not payload:
        return WorkloadSchedulePolicy(workload="hpc", configured=False)
    return WorkloadSchedulePolicy(
        workload="hpc",
        auto_reclaim=_flag(payload.get("enable_auto_stop")),
        reclaim_rule=_parse_rule(payload.get("auto_stop_ruleset")),
        max_runtime_minutes=(
            _timed_minutes(
                payload,
                "max_running_time_days",
                "max_running_time_hours",
                "max_running_time_minutes",
            )
            if _flag(payload.get("enable_max_running_time"))
            else None
        ),
    )


def _serving_policies(payload: dict[str, Any]) -> list[WorkloadSchedulePolicy]:
    if not payload:
        return [WorkloadSchedulePolicy(workload="serving", configured=False)]

    enabled = _flag(payload.get("enable_auto_stop"))
    bands = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    if not bands:
        return [WorkloadSchedulePolicy(workload="serving", auto_reclaim=enabled)]

    rows: list[WorkloadSchedulePolicy] = []
    for band in bands:
        low, high = _int(band.get("gpu_count_min")), _int(band.get("gpu_count_max"))
        rows.append(
            WorkloadSchedulePolicy(
                workload="serving",
                auto_reclaim=enabled,
                reclaim_rule=_parse_rule(band.get("auto_stop_ruleset")),
                applies_to=f"{low}-{high} GPU" if high else None,
            )
        )
    return rows


def get_workspace_schedule_policy(
    workspace_id: str,
    *,
    session: WebSession,
) -> list[WorkloadSchedulePolicy]:
    """Read all workload policies from their three platform-owned records."""
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("workspace_id is required")
    base_url = _get_base_url()

    shared = get_browser_api_action_result(
        _request_json(
            session,
            "POST",
            "/api/v2/notebook?Action=GetScheduleConfig",
            referer=f"{base_url}/jobs/interactiveModeling",
            body={"WorkspaceId": workspace_id},
            timeout=20,
        ),
        action="Get workspace schedule policy",
    )
    hpc = get_browser_api_action_result(
        _request_json(
            session,
            "POST",
            "/api/v2/hpc?Action=GetHpcScheduleConfig",
            referer=f"{base_url}/jobs/highPerformanceComputing",
            body={"workspace_id": workspace_id},
            timeout=20,
        ),
        action="Get HPC schedule policy",
    )
    serving = get_browser_api_action_result(
        _request_json(
            session,
            "POST",
            "/api/v2/inference_serving?Action=GetServingScheduleConfig",
            referer=f"{base_url}/jobs/modelDeployment",
            body={"workspace_id": workspace_id},
            timeout=20,
        ),
        action="Get Serving schedule policy",
    )

    return [
        _notebook_policy(shared),
        _train_policy(shared),
        _hpc_policy(hpc),
        _ray_policy(shared),
        *_serving_policies(serving),
    ]


__all__ = [
    "ReclaimCondition",
    "ReclaimRule",
    "WorkloadSchedulePolicy",
    "format_duration",
    "get_workspace_schedule_policy",
]
