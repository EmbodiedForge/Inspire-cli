"""Quota selection for `inspire notebook create`."""

from __future__ import annotations

import json
from typing import Optional

from inspire.cli.context import Context, EXIT_CONFIG_ERROR
from inspire.cli.utils.errors import exit_with_error as _handle_error

from .helpers import format_resource_display


def resolve_notebook_quota(
    ctx: Context,
    *,
    schedule: dict,
    gpu_count: int,
    gpu_pattern: str,
    requested_cpu_count: Optional[int],
    selected_gpu_type: str,
) -> tuple[str, int, int, str, str] | None:
    """Pick quota and return (quota_id, cpu_count, memory_size, selected_gpu_type, resource_display)."""
    quota_list = schedule.get("quota", [])
    if isinstance(quota_list, str):
        quota_list = json.loads(quota_list) if quota_list else []

    selected_quota = None
    cpu_quotas: list[dict] = []
    if gpu_count == 0:
        cpu_quotas = [q for q in quota_list if q.get("gpu_count", 0) == 0]
        if requested_cpu_count is None:
            for quota in cpu_quotas:
                quota_cpu = quota.get("cpu_count")
                if quota_cpu is None:
                    continue
                if selected_quota is None or quota_cpu < selected_quota.get("cpu_count", 0):
                    selected_quota = quota
            if selected_quota is None and cpu_quotas:
                selected_quota = cpu_quotas[0]
        else:
            for quota in cpu_quotas:
                if quota.get("cpu_count") == requested_cpu_count:
                    selected_quota = quota
                    break
    else:
        for quota in quota_list:
            if quota.get("gpu_type") == selected_gpu_type and quota.get("gpu_count") == gpu_count:
                selected_quota = quota
                break

    if not selected_quota:
        if gpu_count == 0:
            requested_label = (
                f"{requested_cpu_count}xCPU" if requested_cpu_count is not None else "CPU"
            )
            message = f"No quota found for {requested_label}"

            lines: list[str] = []
            for quota in cpu_quotas:
                quota_cpu = quota.get("cpu_count")
                quota_name = quota.get("name")
                label = f"{quota_cpu}xCPU" if quota_cpu else "CPU"
                suffix = f" ({quota_name})" if quota_name else ""
                lines.append(f"  - {label}{suffix}")

            hint = "Available CPU quotas:\n" + "\n".join(lines) if lines else None
        else:
            message = f"No quota found for {gpu_count}x {selected_gpu_type}"

            lines = []
            for quota in quota_list:
                quota_name = quota.get("name")
                suffix = f" ({quota_name})" if quota_name else ""
                lines.append(f"  - {quota.get('gpu_count')}x {quota.get('gpu_type')}{suffix}")

            hint = "Available quotas:\n" + "\n".join(lines) if lines else None

        _handle_error(ctx, "ValidationError", message, EXIT_CONFIG_ERROR, hint=hint)
        return None

    quota_id = selected_quota.get("id", "")
    cpu_count = selected_quota.get("cpu_count", 20)
    memory_size = selected_quota.get("memory_size", 200)

    if gpu_count == 0:
        selected_gpu_type = selected_quota.get("gpu_type", "") or ""

    resource_display = format_resource_display(gpu_count, gpu_pattern, cpu_count)

    return quota_id, cpu_count, memory_size, selected_gpu_type, resource_display


__all__ = ["resolve_notebook_quota"]
