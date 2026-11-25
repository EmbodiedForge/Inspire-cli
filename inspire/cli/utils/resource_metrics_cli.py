"""CLI helpers for rendering platform resource metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_metric_types(values: tuple[str, ...]) -> list[str] | None:
    metrics: list[str] = []
    for value in values:
        metrics.extend(part.strip() for part in str(value).split(",") if part.strip())
    return metrics or None


def _series_points(group: dict[str, Any]) -> list[dict[str, Any]]:
    series = group.get("time_series", [])
    return (
        [point for point in series if isinstance(point, dict)] if isinstance(series, list) else []
    )


def _numeric_values(points: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for point in points:
        raw_value = point.get("data")
        if raw_value is None:
            continue
        try:
            values.append(float(raw_value))
        except (TypeError, ValueError):
            continue
    return values


def summarize_metric_groups(
    groups: list[Any],
    *,
    include_series: bool = False,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        points = _series_points(group)
        values = _numeric_values(points)
        summary: dict[str, Any] = {
            "group_name": group.get("group_name", ""),
            "metric_type": group.get("metric_type", ""),
            "resource_name": group.get("resource_name", ""),
            "points": len(points),
        }
        if values:
            summary["avg"] = sum(values) / len(values)
            summary["max"] = max(values)
            summary["latest"] = values[-1]
        if include_series:
            summary["time_series"] = points
        summaries.append(summary)
    return summaries


def metrics_payload(
    result: dict[str, Any],
    *,
    id_key: str,
    raw: bool = False,
    include_series: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        id_key: result["resource_id"],
        "resource_type": result.get("resource_type"),
        "workspace_id": result.get("workspace_id", ""),
        "logic_compute_group_id": result.get("logic_compute_group_id", ""),
        "task_type": result.get("task_type"),
        "interval_seconds": result.get("interval_seconds"),
        "metric_types": result.get("metric_types", []),
        "start": result.get("start"),
        "end": result.get("end"),
    }
    if result.get("selection"):
        payload["selection"] = result["selection"]
    if result.get("window_clamped"):
        payload["window_clamped"] = result["window_clamped"]
    metrics = result.get("metrics", {})
    if raw:
        payload["metrics"] = metrics
    else:
        payload["metrics"] = {
            metric_type: summarize_metric_groups(groups, include_series=include_series)
            for metric_type, groups in metrics.items()
        }
    return payload


def metrics_text_lines(
    result: dict[str, Any], *, label: str, include_series: bool = False
) -> list[str]:
    lines = [
        f"{label}: {result['resource_id']}",
        f"Task type: {result.get('task_type') or '-'}",
        f"Range: {result.get('start')}..{result.get('end')}",
        f"Interval: {result.get('interval_seconds')}s",
    ]
    selection = result.get("selection")
    if selection:
        parts = [str(selection.get("kind") or "selection")]
        if selection.get("run_index") is not None:
            parts.append(f"run={selection['run_index']}")
        if selection.get("version") is not None:
            parts.append(f"version={selection['version']}")
        lines.append("Selection: " + " ".join(parts))
    clamped = result.get("window_clamped")
    if clamped:
        lines.append(
            "Window: clamped to the newest "
            f"{int(clamped.get('max_span_seconds') or 0) // 3600}h "
            f"({int(clamped.get('max_span_seconds') or 0)}s) - the platform will not sample a "
            f"{clamped.get('interval_seconds')}s interval over a wider window; the resource "
            f"started at {clamped.get('requested_start')}"
        )
    for metric_type, groups in (result.get("metrics") or {}).items():
        lines.append("")
        lines.append(f"{metric_type}:")
        summaries = summarize_metric_groups(groups, include_series=include_series)
        if not summaries:
            lines.append("  (no data)")
            continue
        for summary in summaries:
            group_name = summary.get("group_name") or "?"
            points = summary.get("points", 0)
            if points and "avg" in summary:
                avg = float(summary["avg"])
                max_value = float(summary["max"])
                if "rate" in metric_type:
                    lines.append(
                        f"  {group_name}: avg={avg * 100:.1f}% "
                        f"max={max_value * 100:.1f}% points={points}"
                    )
                else:
                    lines.append(
                        f"  {group_name}: avg={avg:.0f} max={max_value:.0f} points={points}"
                    )
            else:
                lines.append(f"  {group_name}: (no data)")
            if include_series:
                for point in summary.get("time_series", []):
                    timestamp = str(point.get("timestamp") or "")
                    try:
                        timestamp = datetime.fromtimestamp(int(timestamp)).strftime("%H:%M")
                    except (TypeError, ValueError):
                        pass
                    lines.append(f"    {timestamp} {point.get('data')}")
    return lines


__all__ = [
    "metrics_payload",
    "metrics_text_lines",
    "parse_metric_types",
    "summarize_metric_groups",
]
