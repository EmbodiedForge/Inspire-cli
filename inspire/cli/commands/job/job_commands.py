"""Job subcommands (excluding create/logs)."""

from __future__ import annotations

import logging
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import click

from . import job_deps
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.logging_setup import redact_text
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.cli.utils.common import json_option
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_success
from inspire.cli.utils.polling import BlindError, BlindTracker
from inspire.cli.utils.resource_metrics_cli import summarize_metric_groups
from inspire.cli.utils.scheduling_diagnostics import (
    pending_project_slot_hint,
    project_quota_pending_hint,
)
from inspire.cli.utils.timestamps import format_timestamp_utc
from inspire.config import (
    Config,
    ConfigError,
    build_runtime_activate_command,
    build_runtime_cd_command,
    build_runtime_export_commands,
    load_runtime_session_state_for_config,
    resolve_effective_remote_runtime,
)
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.jobs import (
    _select_job_instance,
    canonical_job_status,
    describe_instance,
    is_job_terminal_status,
    native_job_statuses_for_status,
)

# Whether a job is over is asked through `is_job_terminal_status`, and what state it is in through
# `canonical_job_status`; a second table here is what let `job_stopped` go missing from `job wait`.
# What the canonical table cannot answer is keep-alive, because it collapses the retaining and
# reserving spellings onto the plain terminal states, so that one distinction stays spelled out here.
_JOB_KEEP_ALIVE_STATUSES = {
    "FAILED_RETAINING",
    "SUCCEEDED_RETAINING",
    "failed_retaining",
    "succeeded_retaining",
    "job_fail_reserving",
    "job_succeed_reserving",
    "job_success_reserving",
}
_JOB_LIST_DEFAULT_PAGE_SIZE = 50
# The jobs list documents no ceiling; 1000 is the only page_size limit documented for this platform
# (API_ENDPOINTS.md, cluster nodes), so an over-fetched page stops there instead of being refused.
_JOB_LIST_MAX_PAGE_SIZE = 1000
# A client-side filter can require walking past the first page. Stop at 100 pages (5000 rows at the
# default size) so a listing that never reports a total cannot turn into an unbounded scan.
_JOB_LIST_MAX_PAGES = 100
_EVENT_KEYWORDS = (
    "backoff",
    "cancel",
    "err",
    "evict",
    "fail",
    "fatal",
    "forbidden",
    "insufficient",
    "kill",
    "oom",
    "pull",
    "reject",
    "schedul",
    "timeout",
)
_LOG_TEXT_KEYS = ("log", "message", "msg", "content", "line", "text")
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "created_by",
    "cookie",
    "email",
    "institution_id",
    "login_name",
    "set_cookie",
)
_WORKER_NAME_RE = re.compile(r"\bworker-(\d+)\b")
_WORKER_EVENT_REASON_PRIORITY = (
    "PyTorchJobFailed",
    "JobReservingStart",
    "ExitedWithCode",
    "Failed",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized == "created_by_id":
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _job_status_matches(status: Any, statuses: set[str]) -> bool:
    value = str(status or "").strip()
    if not value:
        return False
    return any(candidate in statuses for candidate in (value, value.upper(), value.lower()))


def _is_job_success_status(status: Any) -> bool:
    return canonical_job_status(status) == "SUCCEEDED"


def _is_job_keep_alive_status(status: Any) -> bool:
    return _job_status_matches(status, _JOB_KEEP_ALIVE_STATUSES)


def _status_filter_matches(job_status: Any, statuses: tuple[str, ...] | list[str]) -> bool:
    """True when `job_status` spells one of `statuses`, compared through the canonical table.

    The cache and the list API store different spellings of the same state (`RUNNING` vs
    `job_running`), so an exact string compare silently matches nothing.
    """
    current = canonical_job_status(job_status)
    if not current:
        return False
    return any(current == canonical_job_status(candidate) for candidate in statuses)


def _normalize_job_output_timestamps(job_data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(job_data)
    for field in ("created_at", "finished_at", "instance_started_at"):
        if field in normalized:
            normalized[field] = format_timestamp_utc(normalized[field])
    return normalized


def _platform_job_to_dict(job: Any) -> dict[str, Any]:
    return _normalize_job_output_timestamps(
        {
            "job_id": getattr(job, "job_id", ""),
            "name": getattr(job, "name", ""),
            "status": getattr(job, "status", ""),
            "command": getattr(job, "command", ""),
            "created_at": getattr(job, "created_at", ""),
            "finished_at": getattr(job, "finished_at", None),
            "created_by_name": getattr(job, "created_by_name", ""),
            "created_by_id": getattr(job, "created_by_id", ""),
            "project_id": getattr(job, "project_id", ""),
            "project_name": getattr(job, "project_name", ""),
            "compute_group_name": getattr(job, "compute_group_name", ""),
            "gpu_type": getattr(job, "gpu_type", ""),
            "gpu_count": getattr(job, "gpu_count", 0),
            "instance_count": getattr(job, "instance_count", 1),
            "priority": getattr(job, "priority", 0),
            "priority_level": getattr(job, "priority_level", ""),
            "priority_name": getattr(job, "priority_name", ""),
            "auto_fault_tolerance": getattr(job, "auto_fault_tolerance", False),
            "fault_tolerance_max_retry": getattr(job, "fault_tolerance_max_retry", None),
            "workspace_id": getattr(job, "workspace_id", ""),
        }
    )


def _resolve_job_list_workspace_id(
    config: Config,
    *,
    workspace: str | None,
    workspace_id: str | None,
) -> str | None:
    if workspace_id or workspace:
        return select_workspace_id(
            config,
            explicit_workspace_id=workspace_id,
            explicit_workspace_name=workspace,
        )
    return config.job_workspace_id or config.default_workspace_id


def _project_name_workspace_filter(config: Config, workspace: str | None) -> str | None:
    """Return the project name when --workspace names a configured project.

    Accounts may configure projects under [accounts.<user>.projects.<alias>]
    with a name and a home workspace; projects are visible from every
    workspace, so listing filters client-side by project name instead of
    routing to the project's home workspace.
    """
    key = str(workspace or "").strip()
    if not key:
        return None
    for selector in (config.project_selectors or {}).values():
        if str(selector.get("name") or "").strip() == key:
            return key
    return None


_ACTIVE_EXCLUDED_STATUSES = frozenset({"FAILED", "CANCELLED", "SUCCEEDED"})


def _cache_exclude_statuses(active: bool) -> frozenset[str] | None:
    """The canonical states `--active` drops, or None when the filter is off.

    Canonical tokens, never a spelling: the platform writes different spellings of the same state
    (`job_stopped` vs `CANCELLED`), so a caller compares through `canonical_job_status` instead of
    matching this set's members as strings. Keep-alive spellings are the exception handled by the
    caller: they canonicalize onto these terminal tokens but still hold quota, so they stay listed.
    """
    if not active:
        return None
    return _ACTIVE_EXCLUDED_STATUSES


def _job_is_active_for_list(job: dict[str, Any], active: bool) -> bool:
    excluded = _cache_exclude_statuses(active)
    if not excluded:
        return True
    raw_status = str(job.get("status") or "")
    # A finished round spelled keep-alive (`job_succeed_reserving`, `failed_retaining`, ...) still
    # holds its cards until the retention window expires; dropping it here would hide the one
    # terminal state where `job stop` still buys capacity back.
    if raw_status in _JOB_KEEP_ALIVE_STATUSES:
        return True
    status = canonical_job_status(raw_status)
    return not status or status not in excluded


def _job_matches_status(job: dict[str, Any], status: str | None) -> bool:
    return not status or _status_filter_matches(job.get("status"), [status])


def _list_fetch_page_size(limit: int, *, client_side_filter: bool) -> int:
    """Page size for one list call, over-fetched while a client-side filter discards rows.

    A filter applied after the API answers means fetching exactly `limit` rows hands the filter `limit`
    candidates, which is how `-n 5 --status RUNNING` printed an empty table with rc=0. `--status` now
    goes to the server, so only a project-name filter, `--active`, or a status the platform cannot be
    asked for needs the wider page.
    """
    if limit <= 0:
        return _JOB_LIST_DEFAULT_PAGE_SIZE
    if not client_side_filter:
        return min(limit, _JOB_LIST_MAX_PAGE_SIZE)
    return min(max(limit * 4, limit + 20), _JOB_LIST_MAX_PAGE_SIZE)


def _list_platform_jobs(
    *,
    config: Config,
    limit: int,
    status: str | None,
    active: bool,
    workspace: str | None,
    workspace_id: str | None,
    active_display_filter: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """List platform jobs, applying every filter before deciding whether to fetch another page.

    `status` goes to the server as the platform's own `job_*` spellings, so the page that comes back is
    usually already filtered; a project name, `--active`, and a status the platform cannot be asked for
    are applied here, before the loop decides it has enough rows. A limit is not a reason to stop paging
    while a client-side filter may still discard the rows already fetched.

    `total` counts the rows matching the filters, not the rows `--limit` kept. When the server was given
    every filter it can express, its advertised total is used even if the limit truncates `items`; a
    filter that runs locally forces a bounded walk of the remaining pages so `total` is not the truncated
    page length. Pass `active_display_filter=False` to keep the rows `--active` would drop: a watcher that
    never receives a FAILED row cannot report the job turning FAILED, and it trims its displayed rows
    itself.
    """
    project_filter = None if workspace_id else _project_name_workspace_filter(config, workspace)
    resolved_workspace_id = _resolve_job_list_workspace_id(
        config,
        workspace=None if project_filter else workspace,
        workspace_id=workspace_id,
    )
    native_statuses = native_job_statuses_for_status(status) if status else ()
    local_status_filter = bool(status) and not native_statuses
    client_side_filter = bool(project_filter) or bool(active) or local_status_filter
    # An exact client-filtered total may need several pages; use the platform's maximum page size so
    # a normal workspace needs one or two calls rather than one call per twenty rows.
    page_size = (
        _JOB_LIST_MAX_PAGE_SIZE
        if client_side_filter
        else _list_fetch_page_size(limit, client_side_filter=False)
    )

    def _fetch_page(page_num: int) -> tuple[list[Any], int]:
        if native_statuses:
            return browser_api_module.list_jobs_with_canonical_status(
                status=status,
                workspace_id=resolved_workspace_id,
                page_num=page_num,
                page_size=page_size,
            )
        return browser_api_module.list_jobs(
            workspace_id=resolved_workspace_id,
            page_num=page_num,
            page_size=page_size,
        )

    items: list[dict[str, Any]] = []
    fetched = 0
    page_num = 1
    server_total = 0
    exact_total_required = active_display_filter and (
        bool(project_filter) or bool(active) or local_status_filter
    )
    while True:
        jobs, server_total = _fetch_page(page_num)
        fetched += len(jobs)
        for job in jobs:
            item = _platform_job_to_dict(job)
            if project_filter and item.get("project_name") != project_filter:
                continue
            if not _job_matches_status(item, status):
                continue
            if active_display_filter and active and not _job_is_active_for_list(item, active):
                continue
            items.append(item)

        if not jobs or fetched >= int(server_total or 0) or page_num >= _JOB_LIST_MAX_PAGES:
            break
        if not exact_total_required and limit is not None and limit > 0 and len(items) >= limit:
            break
        page_num += 1

    if exact_total_required:
        total = len(items)
    else:
        total = max(int(server_total or 0), len(items))
    if active_display_filter and limit is not None and limit > 0:
        items = items[:limit]
    return items, total


def _event_summary(event: dict[str, Any]) -> str:
    timestamp = (
        event.get("last_timestamp")
        or event.get("event_time")
        or event.get("time")
        or event.get("created_at")
        or "-"
    )
    reason = event.get("reason") or event.get("type") or event.get("name") or "-"
    message = event.get("message") or event.get("msg") or event.get("note") or ""
    return redact_text(f"{timestamp}  {reason}  {message}".strip())


def _event_is_diagnostic(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or event.get("level") or "").lower()
    if event_type and event_type not in {"normal", "info"}:
        return True
    haystack = _event_summary(event).lower()
    return any(keyword in haystack for keyword in _EVENT_KEYWORDS)


def _select_diagnostic_events(events: list[Any], *, limit: int = 5) -> list[dict[str, Any]]:
    dict_events = [event for event in events if isinstance(event, dict)]
    selected = [event for event in dict_events if _event_is_diagnostic(event)]
    if not selected:
        selected = dict_events
    return [_redact_value(event) for event in selected[-limit:]]


def _event_worker_text(event: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("reason", "message", "msg", "note", "name", "type"):
        value = event.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    for key in ("object", "involved_object", "metadata"):
        value = event.get(key)
        if isinstance(value, dict):
            for nested_key in ("name", "field_path", "resource_name"):
                nested_value = value.get(nested_key)
                if nested_value not in (None, ""):
                    parts.append(str(nested_value))
    return " ".join(parts)


def _route_worker_from_events(events: list[Any]) -> tuple[int | None, str | None]:
    dict_events = [event for event in events if isinstance(event, dict)]
    for preferred_reason in _WORKER_EVENT_REASON_PRIORITY:
        preferred_reason_lower = preferred_reason.lower()
        for event in dict_events:
            event_text = _event_worker_text(event)
            reason = str(event.get("reason") or "")
            if (
                reason.lower() != preferred_reason_lower
                and preferred_reason_lower not in event_text.lower()
            ):
                continue
            match = _WORKER_NAME_RE.search(event_text)
            if match:
                return int(match.group(1)), f"event:{preferred_reason}"

    for event in dict_events:
        match = _WORKER_NAME_RE.search(_event_worker_text(event))
        if match:
            reason = str(event.get("reason") or "").strip()
            return int(match.group(1)), f"event:{reason}" if reason else "event"
    return None, None


def _log_entry_text(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in _LOG_TEXT_KEYS:
            value = entry.get(key)
            if value not in (None, ""):
                return redact_text(str(value))
        return redact_text(str(entry))
    return redact_text(str(entry))


def _extract_log_lines(payload: dict[str, Any], *, limit: int) -> list[str]:
    logs_data = payload.get("logs", []) if isinstance(payload, dict) else []
    if not isinstance(logs_data, list) or limit <= 0:
        return []
    display_logs = list(reversed(logs_data))
    lines = [_log_entry_text(entry) for entry in display_logs]
    return lines[-limit:]


def _parse_wait_worker(worker: str) -> int | None:
    if worker == "auto":
        return None
    try:
        parsed = int(worker)
    except ValueError as exc:
        raise click.BadParameter("must be 'auto' or a non-negative integer") from exc
    if parsed < 0:
        raise click.BadParameter("must be 'auto' or a non-negative integer")
    return parsed


def _collect_event_diagnostics(job_id: str) -> tuple[list[dict[str, Any]], str | None, list[Any]]:
    try:
        payload = browser_api_module.get_job_events(job_id)
        events = payload.get("events", []) if isinstance(payload, dict) else []
        if not isinstance(events, list):
            return [], None, []
        return _select_diagnostic_events(events), None, events
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask wait result.
        return [], redact_text(str(exc)), []


def _safe_job_status(job_id: str) -> str | None:
    """Best-effort job status for a hint. A hint must never turn into the reason a command fails."""
    try:
        detail = browser_api_module.get_job_detail(job_id)
    except Exception:  # noqa: BLE001 - the hint is optional, the empty answer is the finding
        return None
    if not isinstance(detail, dict):
        return None
    job = detail.get("job") if isinstance(detail.get("job"), dict) else detail
    value = job.get("status") if isinstance(job, dict) else None
    return str(value) if value else None


def _collect_single_log_excerpt(
    job_id: str,
    *,
    logs_tail: int,
    worker: int | None,
    instance_name: str | None = None,
) -> dict[str, Any]:
    worker_index = 0 if worker is None else worker
    payload = browser_api_module.get_job_platform_logs(
        job_id,
        page_size=logs_tail,
        worker=worker_index,
        instance_name=instance_name,
        sort="descend",
    )
    return {
        "worker": "auto" if worker is None and instance_name is None else worker_index,
        "instance_name": payload.get("instance_name") or instance_name,
        "lines": _extract_log_lines(payload, limit=logs_tail),
    }


def _collect_log_diagnostics(
    job_id: str,
    *,
    logs_tail: int,
    worker: int | None,
    all_workers: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    if logs_tail <= 0:
        return [], None

    try:
        if not all_workers:
            return [
                _redact_value(
                    _collect_single_log_excerpt(job_id, logs_tail=logs_tail, worker=worker)
                )
            ], None

        instances_payload = browser_api_module.list_job_instances(job_id, page_size=-1)
        instances = (
            instances_payload.get("items", []) if isinstance(instances_payload, dict) else []
        )
        if not isinstance(instances, list) or not instances:
            return [], f"No instances found for job {job_id}"

        excerpts: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in instances:
            if not isinstance(item, dict):
                continue
            instance_name = str(item.get("name") or "")
            if not instance_name:
                continue
            try:
                excerpts.append(
                    _redact_value(
                        _collect_single_log_excerpt(
                            job_id,
                            logs_tail=logs_tail,
                            worker=0,
                            instance_name=instance_name,
                        )
                    )
                )
            except Exception as exc:  # noqa: BLE001 - keep other workers.
                errors.append(f"{instance_name}: {redact_text(str(exc))}")
        return excerpts, "; ".join(errors) if errors else None
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask wait result.
        return [], redact_text(str(exc))


def _build_wait_diagnostics(
    job_id: str,
    job_data: dict[str, Any],
    *,
    elapsed_seconds: int,
    logs_tail: int,
    worker: int | None,
    all_workers: bool,
    show_error: bool,
) -> dict[str, Any]:
    events, events_error, raw_events = _collect_event_diagnostics(job_id)
    selected_worker = worker
    selected_worker_source = "option" if worker is not None else "default"
    if worker is None and not all_workers:
        routed_worker, routed_source = _route_worker_from_events(raw_events)
        if routed_worker is not None:
            selected_worker = routed_worker
            selected_worker_source = routed_source or "event"
        else:
            selected_worker = 0

    logs, logs_error = _collect_log_diagnostics(
        job_id,
        logs_tail=logs_tail,
        worker=selected_worker,
        all_workers=all_workers,
    )
    diagnostics: dict[str, Any] = {
        "job_id": job_id,
        "status": job_data.get("status", "UNKNOWN"),
        "keep_alive": _is_job_keep_alive_status(job_data.get("status")),
        "elapsed_seconds": elapsed_seconds,
        "running_time_ms": job_data.get("running_time_ms"),
        "events": events,
        "log_excerpts": logs,
    }
    if not all_workers and logs_tail > 0:
        diagnostics["selected_worker"] = selected_worker
        diagnostics["selected_worker_source"] = selected_worker_source
    if show_error:
        error_fields = {
            key: job_data.get(key)
            for key in ("error", "error_msg", "message", "msg", "sub_msg", "reason")
            if job_data.get(key) not in (None, "")
        }
        if error_fields:
            diagnostics["job_error"] = _redact_value(error_fields)

    secondary_errors = {}
    if events_error:
        secondary_errors["events"] = events_error
    if logs_error:
        secondary_errors["logs"] = logs_error
    if secondary_errors:
        diagnostics["secondary_errors"] = secondary_errors
    return _redact_value(diagnostics)


def _not_running_reason(job_id: str) -> dict[str, Any] | None:
    """The newest scheduling objection, for jobs that are waiting to exist.

    Measured: a job sat in `instance_creating` for thirteen minutes while `job status` reported only
    `job_creating`, and the answer had been sitting in events the whole time as `Unschedulable`. A caller
    told "creating" cannot distinguish waiting-for-capacity from pulling-a-large-image from a bad spec, so
    the platform's own reason belongs on the status page. Best effort: an event lookup must never be the
    reason a status command fails.

    `Unschedulable` is a state inside queueing, not a terminal admission refusal. The field team's sentinel
    exited on that event judging REFUSED_BY_SCHEDULER, and the job went on to reach `instance_running` on
    another node. A positive instance state therefore cancels the objection before it is quoted, and this
    field is never permission to cancel a job.
    """
    # Positive instance state outranks any scheduling event.
    try:
        instances = browser_api_module.list_job_instances(job_id, page_size=-1)
    except Exception:  # noqa: BLE001 - missing instance data proves nothing either way
        instances = {}
    items = instances.get("items") if isinstance(instances, dict) else None
    if isinstance(items, list):
        for item in items:
            state = (
                str(item.get("instance_status") or item.get("status") or "")
                if isinstance(item, dict)
                else ""
            )
            if state in ("instance_running", "instance_succeeded", "instance_reserving"):
                return None

    try:
        payload = browser_api_module.get_job_events(job_id)
    except Exception:  # noqa: BLE001 - the status answer stands on its own
        return None
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        data = payload.get("data") if isinstance(payload, dict) else None
        events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        text = f"{event.get('reason')} {event.get('message')}"
        if "nschedul" in text or "FailedSchedul" in text or "Insufficient" in text:
            return {
                "reason": str(event.get("reason") or "Unschedulable"),
                "message": str(event.get("message") or "")[:300],
                "from": str(event.get("from") or ""),
                "last_timestamp": event.get("last_timestamp"),
            }
    return None


def _pending_project_quota_hint(job_data: dict[str, Any]) -> str | None:
    """Read the platform's own project quota block when a job is waiting to start.

    Best effort: a quota lookup must never turn a status command into a failure, and a missing
    quota block is not evidence that quota is fine.
    """
    project_id = str(job_data.get("project_id") or "").strip()
    workspace_id = str(job_data.get("workspace_id") or "").strip()
    if not project_id or not workspace_id:
        return None
    try:
        payload = browser_api_module.get_project_quota_jobs(
            workspace_id=workspace_id,
            project_id=project_id,
        )
    except Exception:  # noqa: BLE001 - status stands without the quota view
        return None
    quota = payload.get("quota") if isinstance(payload, dict) else None
    return project_quota_pending_hint(quota, job_data.get("project_name"))


def _format_wait_diagnostics(diagnostics: dict[str, Any]) -> str:
    lines = ["Diagnostics"]
    running_time = diagnostics.get("running_time_ms")
    if running_time not in (None, ""):
        lines.append(
            "Running Time: "
            + human_formatter._format_running_with_limit(
                running_time, diagnostics.get("max_running_time_ms")
            )
        )

    job_error = diagnostics.get("job_error")
    if isinstance(job_error, dict) and job_error:
        lines.append("Job Error:")
        for key, value in job_error.items():
            lines.append(f"- {key}: {value}")

    events = diagnostics.get("events")
    if isinstance(events, list) and events:
        lines.append("Events:")
        for event in events:
            if isinstance(event, dict):
                lines.append(f"- {_event_summary(event)}")

    log_excerpts = diagnostics.get("log_excerpts")
    if isinstance(log_excerpts, list) and log_excerpts:
        lines.append("Platform Logs:")
        for excerpt in log_excerpts:
            if not isinstance(excerpt, dict):
                continue
            label_parts = []
            if excerpt.get("worker") not in (None, ""):
                label_parts.append(f"worker {excerpt['worker']}")
            if excerpt.get("instance_name"):
                label_parts.append(f"instance {excerpt['instance_name']}")
            label = ", ".join(label_parts) or "selected worker"
            lines.append(f"- {label}:")
            excerpt_lines = excerpt.get("lines", [])
            if isinstance(excerpt_lines, list) and excerpt_lines:
                for line in excerpt_lines:
                    lines.append(f"  {line}")
            else:
                lines.append("  <no platform log lines returned>")

    secondary_errors = diagnostics.get("secondary_errors")
    if isinstance(secondary_errors, dict) and secondary_errors:
        lines.append("Diagnostic lookup warnings:")
        for key, value in secondary_errors.items():
            lines.append(f"- {key}: {value}")

    return "\n".join(lines)


def _normalize_exec_command(command_parts: tuple[str, ...]) -> str:
    if not command_parts:
        raise click.UsageError("Provide a command to execute.")
    if len(command_parts) == 1:
        return command_parts[0]
    return shlex.join(command_parts)


def _job_exec_runtime_prefix(ctx: Context) -> list[str] | None:
    """Shell commands putting ``job exec`` in the environment every other exec path already has.

    The same command line used to resolve differently depending on which verb reached it: ``run``,
    ``job create``, ``bridge exec`` and ``notebook exec`` apply ``[remote_env]`` (package index,
    proxies), ``path_prepend``, ``venv`` and ``cwd``, while ``job exec`` - the verb used most while
    debugging a training job - applied none of it. The transport execs a non-login bash and then
    clears ``PS1``/``PS0``/``PROMPT_COMMAND``, so shell startup files do not supply it either; that
    is why probing a job needed ``bash -lc`` and absolute paths.

    Returns None when the configuration cannot be loaded, so the caller fails instead of running a
    command in a silently different environment - which is the failure mode this exists to remove.
    """
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
        return None

    runtime = resolve_effective_remote_runtime(
        config,
        require_cwd=False,
        session_state=load_runtime_session_state_for_config(config),
    )
    # Same layering as job submission: env, then PATH, then venv, then working directory.
    parts = build_runtime_export_commands(runtime)
    activate_command = build_runtime_activate_command(runtime)
    if activate_command:
        parts.append(activate_command)
    cd_command = build_runtime_cd_command(runtime)
    if cd_command:
        parts.append(cd_command)
    return parts


def _watch_jobs(
    ctx: Context,
    config: Config,
    limit: int,
    status: Optional[str],
    active: bool,
    interval: int,
    workspace: str | None = None,
    workspace_id: str | None = None,
) -> None:
    """Continuously poll and display jobs from the platform list API."""
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    if not ctx.json_output:
        click.echo("🔐 Authenticating...")

    completed_this_session: list = []
    completed_job_ids: set = set()
    last_status_by_id: dict[str, str] = {}

    def _progress_bar(current: int, total: int, width: int = 20) -> str:
        if total == 0:
            return "░" * width
        filled = int(width * current / total)
        return "█" * filled + "░" * (width - filled)

    def _render_display(
        jobs_list: list,
        updated_count: int,
        total_count: int,
        completed_list: list,
    ) -> None:
        safe_jobs_list = _redact_value(jobs_list)
        safe_completed_list = _redact_value(completed_list)
        if not ctx.json_output:
            os.system("clear")
        if ctx.json_output:
            timestamp = datetime.now().strftime("%H:%M:%S")
            click.echo(
                json_formatter.format_json(
                    {
                        "event": "refresh",
                        "timestamp": timestamp,
                        "updated": updated_count,
                        "total": total_count,
                        "items": safe_jobs_list,
                        "completed_this_session": safe_completed_list,
                    }
                )
            )
        else:
            bar = _progress_bar(updated_count, total_count)
            if updated_count < total_count:
                click.echo(f"🔄 [{bar}] {updated_count}/{total_count} updating...\n")
            else:
                click.echo(f"✅ [{bar}] {total_count}/{total_count} done (interval: {interval}s)\n")

            click.echo(human_formatter.format_job_list(safe_jobs_list))

            if safe_completed_list:
                click.echo(f"\n✅ Completed This Session ({len(safe_completed_list)})")
                click.echo("─" * 60)
                for job_item in safe_completed_list:
                    status_emoji = (
                        "✅" if "succeeded" in job_item.get("status", "").lower() else "❌"
                    )
                    click.echo(
                        f"{job_item.get('job_id', 'N/A')[:36]:36}  "
                        f"{job_item.get('name', 'N/A')[:20]:20}  "
                        f"{status_emoji} {job_item.get('status', 'N/A')}"
                    )

    try:
        while True:
            # The transition scan runs over every fetched row. Applying `--active` here instead would
            # drop FAILED/CANCELLED before the comparison below could ever see a job turn over, so
            # "Completed This Session" would only ever count successes; the flag belongs to the display.
            jobs, total = _list_platform_jobs(
                config=config,
                limit=limit,
                status=status,
                active=active,
                workspace=workspace,
                workspace_id=workspace_id,
                active_display_filter=False,
            )
            for job_item in jobs:
                job_id = str(job_item.get("job_id") or "")
                if not job_id:
                    continue
                original_status = last_status_by_id.get(job_id, "")
                new_status = str(job_item.get("status") or "")
                if (
                    is_job_terminal_status(new_status)
                    and original_status
                    and not is_job_terminal_status(original_status)
                    and job_id not in completed_job_ids
                ):
                    completed_this_session.append(dict(job_item))
                    completed_job_ids.add(job_id)
                if new_status:
                    last_status_by_id[job_id] = new_status

            display_jobs = [
                job_item for job_item in jobs if _job_is_active_for_list(job_item, active)
            ]
            if limit is not None and limit > 0:
                display_jobs = display_jobs[:limit]
            _render_display(display_jobs, len(display_jobs), total, completed_this_session)
            job_deps.time.sleep(interval)

    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nStopped watching.")
        sys.exit(EXIT_SUCCESS)
    finally:
        api_logger.setLevel(original_level)


@click.command("list")
@click.option("--limit", "-n", type=int, default=10, help="Max jobs to show (default: 10)")
@click.option("--status", "-s", help="Filter by status (PENDING, RUNNING, SUCCEEDED, FAILED)")
@click.option(
    "--active",
    "-a",
    is_flag=True,
    help="Show only active training jobs (drops terminal succeeded/failed/cancelled; "
    "finished-but-reserving rounds stay listed because they still hold quota)",
)
@click.option("--watch", "-w", is_flag=True, help="Continuously refresh job list")
@click.option(
    "--interval",
    type=int,
    default=10,
    help="Refresh interval in seconds for --watch (default: 10)",
)
@click.option(
    "--workspace",
    help="Workspace alias, ID, or configured project name for platform listing.",
)
@click.option("--workspace-id", help="Explicit workspace ID for platform listing.")
@click.option(
    "--all-users",
    is_flag=True,
    help="Compatibility no-op: ListJobs already includes training jobs visible in the workspace.",
)
@json_option
@pass_context
def list_jobs(
    ctx: Context,
    limit: int,
    status: Optional[str],
    active: bool,
    watch: bool,
    interval: int,
    workspace: str | None,
    workspace_id: str | None,
    all_users: bool,
    json_output: bool = False,
) -> None:
    """List recent training-job history visible in the workspace.

    This reads the training service's ListJobs history. It does not include
    notebooks, inference servings, or the console live task view; use
    inspire notebook list for notebooks.

    \b
    Example:
        inspire job list
        inspire job list --limit 20 --status RUNNING
        inspire job list --active
        inspire job list --workspace gpu
        inspire job list --watch --active -n 20
    """

    json_output = resolve_json_output(ctx, json_output)
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)

        if watch:
            _watch_jobs(
                ctx=ctx,
                config=config,
                limit=limit,
                status=status,
                active=active,
                interval=interval,
                workspace=workspace,
                workspace_id=workspace_id,
            )
            return

        jobs, total = _list_platform_jobs(
            config=config,
            limit=limit,
            status=status,
            active=active,
            workspace=workspace,
            workspace_id=workspace_id,
        )

        safe_jobs = _redact_value(jobs)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"items": safe_jobs, "total": total if total is not None else len(jobs)}
                )
            )
        else:
            click.echo(human_formatter.format_job_list(safe_jobs))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _attempt_accounting(job_id: str, job_data: dict[str, Any]) -> dict[str, Any] | None:
    """Classified attempt history for a job, or None when the platform will not tell us.

    Best effort on purpose: `job status` is the command people run when something is already wrong, and
    an unavailable instance list must not turn a working status read into an error (issue #31).

    The job's own keep-alive windows ride along so a reserving round can state when it gives its card
    back; whichever key the record omits simply stays unreported.
    """
    try:
        items = browser_api_module.list_job_instances(job_id, page_size=-1).get("items") or []
    except Exception:  # noqa: BLE001 - reporting this loudly would hide the status people came for
        return None
    return browser_api_module.summarize_attempts(
        [item for item in items if isinstance(item, dict)],
        reserve_on_fail_ms=job_data.get("reserve_on_fail_ms"),
        reserve_on_success_ms=job_data.get("reserve_on_success_ms"),
    )


def _local_deadline(value: Any) -> str | None:
    """A `…T…Z` instant as the wall clock of the machine reading it, or None when it is not one.

    Window expiry is a time someone acts on, so it is printed in their own zone; the payload keeps
    UTC so two people in different zones can still compare notes.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment.astimezone().strftime("%H:%M:%S (%z)")


def _card_hold_notice(job_id: str, reserving: int, reserve_until: Any) -> str:
    """The one state where `job stop` still buys money back, with its deadline when we know it."""
    until = _local_deadline(reserve_until)
    if until:
        if reserving == 1:
            return (
                f"{reserving} round still holds its card until {until}: "
                f"`inspire job stop {job_id}` returns it now"
            )
        return (
            f"{reserving} rounds still hold their cards until {until}: "
            f"`inspire job stop {job_id}` returns them now"
        )
    if reserving == 1:
        return (
            f"{reserving} round still holds its card: "
            f"`inspire job stop {job_id}` gives it back now instead of at window expiry"
        )
    return (
        f"{reserving} rounds still hold their cards: "
        f"`inspire job stop {job_id}` gives them back now instead of at window expiry"
    )


@click.command("status")
@click.argument("job_id")
@json_option
@pass_context
def status(ctx: Context, job_id: str, json_output: bool = False) -> None:
    """Check the status of a training job.

    \b
    Example:
        inspire job status <job-id>
    """

    json_output = resolve_json_output(ctx, json_output)
    job_id = resolve_job_id(ctx, job_id)

    try:
        # Resolved for its side effects only: a broken layered configuration must still leave through
        # the config error path below, before any request is attempted.
        Config.from_files_and_env(
            require_target_dir=False,
            require_credentials=False,
        )
        result = browser_api_module.get_job_detail(job_id)
        job_data = result.get("data", {})

        safe_job_data = _normalize_job_output_timestamps(_redact_value(job_data))
        accounting = _attempt_accounting(job_id, job_data)

        # Only for jobs that have not started: for a running job this is noise, and it costs a request.
        pending_reason = None
        if str(job_data.get("status") or "") in (
            "job_queuing",
            "job_pending",
            "job_creating",
            "job_restarting",
        ):
            pending_reason = _not_running_reason(job_id)
        pending_hint = None
        if pending_reason is None:
            pending_hint = _pending_project_quota_hint(job_data)
            if pending_hint is None:
                pending_hint = pending_project_slot_hint(
                    job_data.get("status"), job_data.get("project_name")
                )

        if ctx.json_output:
            if accounting is not None:
                safe_job_data["attempt_accounting"] = accounting
            # The canonical table collapses the reserving spellings onto plain terminal states, so a
            # reader that only looks at `status` cannot see that stopping this job still returns
            # capacity. `job wait` has said so for a while; `status` did not.
            safe_job_data["keep_alive"] = _is_job_keep_alive_status(job_data.get("status"))
            if pending_reason:
                safe_job_data["not_running_reason"] = pending_reason
            if pending_hint:
                safe_job_data["pending_hint"] = pending_hint
            click.echo(json_formatter.format_json(safe_job_data))
        else:
            click.echo(human_formatter.format_job_status(safe_job_data))
            if pending_reason:
                click.secho(
                    f"Why it is not running yet: {pending_reason['reason']} - {pending_reason['message']}"
                    f" (from {pending_reason['from'] or 'scheduler'})",
                    fg="yellow",
                    err=True,
                )
            if pending_hint:
                click.secho(f"Pending note: {pending_hint}", fg="yellow", err=True)
            if accounting is None:
                click.secho(
                    "attempt accounting unavailable (instance list failed)", err=True, dim=True
                )
            elif accounting.get("total"):
                click.echo(human_formatter.format_attempt_summary(accounting))
                reserving = (accounting.get("counts") or {}).get("reserving", 0)
                if reserving:
                    # The one state where `job stop` still buys money back, and it is invisible in the
                    # job-level status field.
                    click.secho(
                        _card_hold_notice(job_id, reserving, accounting.get("reserving_until")),
                        fg="yellow",
                    )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("job_id")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Stop without an interactive confirmation (alias: --force)",
)
@json_option
@pass_context
def stop(ctx: Context, job_id: str, yes: bool, json_output: bool = False) -> None:
    """Stop a running training job.

    \b
    Example:
        inspire job stop <job-id>
        inspire job stop <job-id> --yes
    """

    # The confirmation comes before any work: a caller that cannot answer a prompt should hear the
    # refusal rather than pay for an ID resolution it was never going to use.
    if not confirmed_or_error(ctx, assume_yes=yes, subject=f"stop job {job_id}"):
        raise click.Abort()

    json_output = resolve_json_output(ctx, json_output)
    job_id = resolve_job_id(ctx, job_id)

    try:
        # Validate the account selection before the mutation; the stop itself keeps no local state.
        Config.from_files_and_env()
        browser_api_module.stop_training_job(job_id)

        emit_success(
            ctx,
            payload={"job_id": job_id, "status": "stopped"},
            text=f"Job stopped: {job_id}",
        )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


_PHASE_AGE_KEYS = ("phase_since", "phase_age_seconds")


def _instances_payload_with_phase_age(payload: dict[str, Any]) -> dict[str, Any]:
    """Add the phase-age keys to the platform's own instance rows, changing nothing else.

    `describe_instance` is the single place that decides what evidence supports an age (issue #61), but its
    output is a *normalised* row while this command has always echoed the platform's raw one. Merging the
    keys in keeps every existing field intact, including the ones a caller reads directly off the row.
    """
    items = payload.get("items")
    if not isinstance(items, list):
        return payload
    enriched: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        described = describe_instance(item)
        merged = dict(item)
        for key in _PHASE_AGE_KEYS:
            if key in described:
                merged[key] = described[key]
        enriched.append(merged)
    merged_payload = dict(payload)
    merged_payload["items"] = enriched
    return merged_payload


def _format_phase_age(seconds: int) -> str:
    """Compact time-since-scheduling for the instances table: ``3h12m``, ``12m``, ``0m``."""
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes}m" if hours else f"{minutes}m"


@click.command("instances")
@click.argument("job_id")
@click.option("--page-size", type=int, default=100, help="Max instances to fetch.")
@json_option
@pass_context
def instances(
    ctx: Context,
    job_id: str,
    page_size: int,
    json_output: bool = False,
) -> None:
    """List platform instances for a training job."""

    json_output = resolve_json_output(ctx, json_output)
    job_id = resolve_job_id(ctx, job_id)
    try:
        payload = browser_api_module.list_job_instances(job_id, page_size=page_size)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not items:
        # Zero rows is the state a freshly created job is in before it is scheduled, and reporting it as a
        # bare "no instances" reads as "this job does not exist" - the same family as the generic 100003 and
        # as an empty default view. Name the job's own status so the reader can tell "not scheduled yet" from
        # "already finished and reaped".
        status = _safe_job_status(job_id)
        payload = dict(payload)
        payload["job_status"] = status
        payload["zero_rows_means"] = (
            "no instance recorded yet for this job"
            if status in (None, "job_queuing", "job_pending", "job_creating")
            else f"no instance available while the job reports {status}"
        )
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
            return
        detail = f" The job reports status `{status}`." if status else ""
        click.echo(
            f"No instances found for {job_id}. Zero rows means no instance has been recorded - "
            f"queuing jobs legitimately have none yet.{detail} It is not evidence the job is absent; "
            "`inspire job status` answers that question."
        )
        return

    if json_output:
        click.echo(json_formatter.format_json(_instances_payload_with_phase_age(payload)))
        return

    for item in items:
        if not isinstance(item, dict):
            continue
        phase_age = describe_instance(item).get("phase_age_seconds")
        age_text = f"(for {_format_phase_age(phase_age)})" if isinstance(phase_age, int) else ""
        click.echo(
            f"{item.get('name', '-'):<54} "
            f"{item.get('instance_status') or item.get('status') or '-':<18} "
            f"{age_text:<13} "
            f"{item.get('node') or item.get('node_name') or '-'}"
        )


@click.command("events")
@click.argument("job_id")
@json_option
@pass_context
def events(ctx: Context, job_id: str, json_output: bool = False) -> None:
    """List scheduler or Kubernetes events for a training job."""

    json_output = resolve_json_output(ctx, json_output)
    job_id = resolve_job_id(ctx, job_id)
    try:
        payload = browser_api_module.get_job_events(job_id)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        events_list = payload.get("events", []) if isinstance(payload, dict) else []
        click.echo(json_formatter.format_json({"items": events_list, "total": len(events_list)}))
        return

    events_data = payload.get("events", []) if isinstance(payload, dict) else []
    if not events_data:
        click.echo(f"No events found for {job_id}.")
        return
    for event in events_data:
        if not isinstance(event, dict):
            continue
        reason = event.get("reason") or event.get("type") or "-"
        message = event.get("message") or event.get("msg") or ""
        timestamp = (
            event.get("last_timestamp") or event.get("event_time") or event.get("time") or "-"
        )
        click.echo(f"{timestamp}  {reason}  {message}")


@click.command("metrics")
@click.argument("job_id")
@click.option(
    "--metric",
    "metric_types",
    multiple=True,
    help=(
        "Metric type to fetch. Repeatable; defaults to GPU usage and GPU memory usage "
        "(gpu_usage_rate, gpu_memory_usage_rate) — pass memory_usage_rate/cpu_usage_rate "
        "explicitly for the container cgroup series."
    ),
)
@click.option(
    "--interval",
    "interval_seconds",
    type=int,
    default=300,
    show_default=True,
    help="Metric interval in seconds.",
)
@click.option(
    "--raw",
    is_flag=True,
    help="Emit the platform payload verbatim, including its `time_seris_metric_groups` key (note the typo).",
)
@json_option
@pass_context
def metrics(
    ctx: Context,
    job_id: str,
    metric_types: tuple[str, ...],
    interval_seconds: int,
    raw: bool = False,
    json_output: bool = False,
) -> None:
    """Fetch platform time-series metrics for a training job."""
    json_output = resolve_json_output(ctx, json_output)
    if interval_seconds <= 0:
        _handle_error(ctx, "ValidationError", "--interval must be positive", EXIT_VALIDATION_ERROR)
        return

    job_id = resolve_job_id(ctx, job_id)
    try:
        payload = browser_api_module.get_job_resource_metrics(
            job_id,
            metric_types=list(metric_types) or None,
            interval_seconds=interval_seconds,
        )
    except Exception as e:
        text = str(e)
        hint = None
        if "invalid value for enum" in text.lower():
            # The platform answers a bad metric name with a protobuf complaint about an enum field, which
            # tells nobody that four values are valid - and one of them is the memory series people need
            # for OOM post-mortems (issue #44).
            hint = (
                "accepted metric types: gpu_usage_rate, gpu_memory_usage_rate, memory_usage_rate, "
                "cpu_usage_rate. memory_usage_rate is a ratio, not bytes - read the container's own "
                "/sys/fs/cgroup/memory.max before converting, and treat usage as an upper bound because "
                "page cache is counted (issue #44)"
            )
        _handle_error(ctx, "APIError", text, EXIT_API_ERROR, hint=hint)
        return

    if ctx.json_output:
        if raw:
            click.echo(json_formatter.format_json(payload))
            return
        # By default this command returned the platform payload verbatim, so `--json` printed a single
        # top-level key named `time_seris_metric_groups` - typo included - while the human table said
        # "36 point(s)". A reader who guessed the sane spelling got None and concluded the series was empty.
        # Notebooks, models and Ray jobs already summarise through the same helper; jobs now match them.
        normalised = dict(payload)
        normalised["metrics"] = {
            metric_type: summarize_metric_groups(_metric_groups(metric_payload))
            for metric_type, metric_payload in (payload.get("metrics") or {}).items()
        }
        normalised["metric_shape"] = (
            "each entry is one series: points is the sample count, avg/max/latest are over the returned "
            "points. Values are ratios, not bytes - see issue #44 for what the two terms actually are. "
            "Pass --raw for the verbatim platform payload."
        )
        click.echo(json_formatter.format_json(normalised))
        return

    click.echo(f"Job: {payload.get('job_id', job_id)}")
    click.echo(f"Task type: {payload.get('task_type', '-')}")
    for metric_type, metric_payload in (payload.get("metrics") or {}).items():
        groups = _metric_groups(metric_payload)
        point_count = sum(
            len(group.get("time_series", [])) for group in groups if isinstance(group, dict)
        )
        click.echo(
            f"{metric_type}: {point_count} points across {len(groups)} series "
            "(values are ratios; --json gives per-series avg/max/latest)"
        )


def _metric_groups(metric_payload: object) -> list[Any]:
    """Unwrap one metric's group list, tolerating the platform's misspelled key.

    Both spellings are accepted because the wire name is `time_seris_metric_groups`; a caller who fixes the
    typo in their own code reads None and concludes no data was returned.
    """
    if isinstance(metric_payload, list):
        return [group for group in metric_payload if isinstance(group, dict)]
    if isinstance(metric_payload, dict):
        groups = metric_payload.get("time_seris_metric_groups") or metric_payload.get(
            "time_series_metric_groups"
        )
        if isinstance(groups, list):
            return [group for group in groups if isinstance(group, dict)]
    return []


def parse_worker_spec(spec: str) -> list[int] | None:
    """Parse a ``--workers`` spec: ``all``, or worker indices like ``0,3-5``.

    Returns ``None`` for ``all`` and a sorted, de-duplicated index list otherwise. Raises
    ``ValueError`` with the offending element named, so a typo reads as the caller's input
    and not as a platform failure.
    """
    text = str(spec or "").strip().lower()
    if not text:
        raise ValueError("--workers needs 'all' or worker indices like '0,3-5'")
    if text == "all":
        return None
    indices: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"--workers element {part!r} is empty; use '0,3-5' or 'all'")
        match = re.fullmatch(r"(\d+)-(\d+)", part)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if high < low:
                raise ValueError(f"--workers range {part!r} is backwards")
            indices.update(range(low, high + 1))
        elif part.isdigit():
            indices.add(int(part))
        else:
            raise ValueError(
                f"--workers element {part!r} is not a worker index or range; use '0,3-5' or 'all'"
            )
    if not indices:
        raise ValueError("--workers selected no workers")
    return sorted(indices)


def _utc_now_iso() -> str:
    """Stamped on every verdict so a snapshot cannot be mistaken for 'now' (issue #41)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_epoch(value: float | None) -> str:
    """Where the last observation happened, or `never` when the loop has not had one.

    'Since when has this been blind' is the question a reader asks first, and 'we never saw it at
    all' is a different answer from a timestamp (issue #59).
    """
    if value is None:
        return "never"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_instance_row(job_id: str) -> dict[str, Any]:
    """Newest attempt row, or {} when the platform has none yet."""
    try:
        items = browser_api_module.list_job_instances(job_id, page_size=-1).get("items") or []
    except Exception:  # noqa: BLE001 - a missing instance list is itself evidence, not an error
        return {}
    rows = [item for item in items if isinstance(item, dict)]
    if not rows:
        return {}
    try:
        # A re-run job has one row per fault-tolerance attempt and the list carries no ordering
        # promise, so the first row is a coin flip between a dead attempt and the live one.
        return _select_job_instance(rows, worker=0, instance_name=None)
    except ValueError:
        # No worker-0 row to read - "no evidence", which is what the wait conditions expect when the
        # platform has told us nothing about the attempt they are judging.
        return {}


def _latest_unschedulable(job_id: str) -> dict[str, Any]:
    try:
        events = browser_api_module.list_job_events(job_id)
    except Exception:  # noqa: BLE001
        return {"available": False}
    stamps: list[tuple[int, str]] = []
    for event in events:
        if not isinstance(event, dict) or str(event.get("reason")) != "Unschedulable":
            continue
        raw = str(event.get("last_timestamp") or event.get("first_timestamp") or "")
        message = " ".join(str(event.get("message") or "").split())
        stamps.append((int(raw) if raw.isdigit() else 0, message))
    if not stamps:
        return {"available": True, "count": 0, "last_message": None, "last_timestamp": None}
    stamps.sort(key=lambda item: item[0])
    stamp, message = stamps[-1]
    return {
        "available": True,
        "count": len(stamps),
        "last_message": message,
        "last_timestamp": stamp or None,
    }


def _wait_condition_hit(
    condition: str, job_id: str, job_data: dict[str, Any]
) -> dict[str, Any] | None:
    """Evaluate `--until` and return the inputs behind the verdict, never the verdict alone.

    The evidence travels with the conclusion because `Unschedulable` on its own is not a stuck job: on
    `arms-16` six Unschedulable events coexisted with a container that ran fine for 3s. A reader has to be
    able to disagree with the derivation, which is the thing polling human prose never allowed (issue #41).
    """
    status = str(job_data.get("status") or "")
    row = _first_instance_row(job_id)
    started = str(row.get("started_at") or "").strip()
    running_job = "running" in status.lower()

    if condition == "running":
        if not started:
            return None
        return {"job_status": status, "instance_started_at": started, "instance": row}

    if condition == "failed":
        terminal_failed = is_job_terminal_status(status) and not _is_job_success_status(status)
        if not (terminal_failed or str(row.get("instance_status")) == "instance_failed"):
            return None
        return {"job_status": status, "instance": row}

    if condition == "unschedulable":
        if started or running_job:
            # Something is executing; Unschedulable lines belong to other replicas or an earlier round.
            return None
        events = _latest_unschedulable(job_id)
        if not events.get("available") or not events.get("count"):
            return None
        return {
            "job_status": status,
            "instance": row,
            "no_instance_has_started": True,
            "unschedulable": events,
        }

    return None  # pragma: no cover - Click restricts the choices


def _render_condition_hit(condition: str, evidence: dict[str, Any]) -> None:
    row = evidence.get("instance") or {}
    click.echo(f"condition hit: {condition}  as of {evidence.get('as_of_utc')}")
    click.echo(f"  job status: {evidence.get('job_status')}")
    click.echo(
        "  instance: "
        f"{row.get('name') or '<none>'} [{row.get('instance_status') or '<none>'}] "
        f"node={row.get('node') or '<none>'} started_at={row.get('started_at') or '<none>'} "
        f"round={row.get('running_round', '<none>')}"
    )
    unsched = evidence.get("unschedulable")
    if isinstance(unsched, dict):
        click.echo(f"  unschedulable events: {unsched.get('count')} (no instance has started)")
        click.echo(f"  last verdict: {unsched.get('last_message')}")


@click.command("wait")
@click.argument("job_id")
@click.option("--timeout", type=int, default=14400, help="Timeout in seconds (default: 4 hours)")
@click.option("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
@click.option(
    "--logs-tail",
    type=int,
    default=20,
    show_default=True,
    help="Platform log lines to include when the job fails; use 0 to disable.",
)
@click.option(
    "--logs-tail-all-workers",
    is_flag=True,
    help="On failure, include platform log excerpts for every listed job instance.",
)
@click.option(
    "--worker",
    default="auto",
    show_default=True,
    help="Worker index for failure log diagnostics, or 'auto'.",
)
@click.option(
    "--until",
    type=click.Choice(["terminal", "running", "failed", "unschedulable"]),
    default="terminal",
    show_default=True,
    help="Stop as soon as this condition holds instead of waiting for a terminal state. `running` means "
    "an instance reported started_at, not that the job status looks busy.",
)
@click.option(
    "--show-error",
    is_flag=True,
    help="Include backend error/message fields in failure diagnostics.",
)
@json_option
@pass_context
def wait(
    ctx: Context,
    job_id: str,
    timeout: int,
    interval: int,
    logs_tail: int,
    logs_tail_all_workers: bool,
    worker: str,
    show_error: bool,
    until: str,
    json_output: bool = False,
) -> None:
    """Wait for a job to complete, or for one specific condition to become true.

    Polls the job status until it reaches a terminal state
    (SUCCEEDED, FAILED, or CANCELLED).

    \b
    Example:
        inspire job wait <job-id> --timeout 7200
    """

    json_output = resolve_json_output(ctx, json_output)
    job_id = resolve_job_id(ctx, job_id)
    if logs_tail < 0:
        raise click.BadParameter("--logs-tail must be non-negative")
    if timeout <= 0:
        _handle_error(ctx, "ValidationError", "--timeout must be positive", EXIT_VALIDATION_ERROR)
        return
    if interval < 1:
        # One poll is up to three platform calls. `--interval 0` turns a wait into a hot loop against the
        # control plane, which is the opposite of what a user pacing a cluster is trying to do; measured
        # with a test double it polled ~500k times in 30s, and against the real API that is a self-inflicted
        # rate-limit event that gets the account noticed.
        _handle_error(
            ctx,
            "ValidationError",
            "--interval must be at least 1 second",
            EXIT_VALIDATION_ERROR,
            hint="each poll costs up to three platform calls; 30s is the default for a reason",
        )
        return
    worker_index = _parse_wait_worker(worker)

    try:
        # Validate the account selection once up front; every poll below reads live detail.
        Config.from_files_and_env()
        start_time = job_deps.time.time()
        last_status = None
        status_events: list[dict[str, Any]] = []
        # Three failed reads in a row is not a quiet job, it is this client going blind. Without the
        # counter a dead network printed one warning per poll and looked exactly like a job that was
        # simply taking its time, until --timeout expired 4 hours later (issue #59).
        blind = BlindTracker()

        if not ctx.json_output:
            # Progress, not the answer: stderr, so a piped stdout carries only the verdict.
            click.echo(
                f"Waiting for job {job_id} (timeout: {timeout}s, interval: {interval}s)", err=True
            )

        while True:
            elapsed = job_deps.time.time() - start_time

            if elapsed > timeout:
                # Exit 14 means "this client stopped waiting", which is not "the condition is false" and
                # not "the job is broken" - the three must stay distinguishable or a watcher that dies
                # looks like a job that ended.
                _handle_error(
                    ctx,
                    "Timeout",
                    f"Timeout after {timeout}s waiting for condition '{until}'",
                    EXIT_TIMEOUT,
                    hint=(
                        "this is the client giving up, not a verdict on the job. Re-run with a longer "
                        "--timeout, and read the last status line to see what it was doing"
                    ),
                )
                return

            try:
                result = browser_api_module.get_job_detail(job_id)
                blind.record_success()
                job_data = result.get("data", {})
                current_status = job_data.get("status", "UNKNOWN")

                if current_status != last_status:
                    if ctx.json_output:
                        status_events.append(
                            {
                                "event": "status_change",
                                "status": current_status,
                                "elapsed_seconds": int(elapsed),
                            }
                        )
                    else:
                        click.echo(f"\nStatus: {current_status}", err=True)
                    last_status = current_status
                else:
                    if not ctx.json_output:
                        mins = int(elapsed // 60)
                        secs = int(elapsed % 60)
                        click.echo(
                            f"\r[{mins:02d}:{secs:02d}] Waiting... Status: {current_status}",
                            nl=False,
                            err=True,
                        )

                if until != "terminal":
                    hit = _wait_condition_hit(until, job_id, job_data)
                    if hit is not None:
                        hit = _normalize_job_output_timestamps(hit)
                        hit["as_of_utc"] = _utc_now_iso()
                        hit["condition"] = until
                        hit["waited_seconds"] = int(elapsed)
                        if ctx.json_output:
                            click.echo(json_formatter.format_json(hit, success=True))
                        else:
                            # Ends the spinner line, which lives on stderr.
                            click.echo("", err=True)
                            _render_condition_hit(until, hit)
                        return

                if is_job_terminal_status(current_status):
                    safe_job_data = _normalize_job_output_timestamps(_redact_value(job_data))
                    wait_succeeded = _is_job_success_status(current_status)
                    safe_job_data["keep_alive"] = _is_job_keep_alive_status(current_status)
                    if ctx.json_output:
                        safe_job_data["wait_events"] = status_events

                    if not wait_succeeded:
                        safe_job_data["wait_diagnostics"] = _build_wait_diagnostics(
                            job_id,
                            job_data,
                            elapsed_seconds=int(elapsed),
                            logs_tail=logs_tail,
                            worker=worker_index,
                            all_workers=logs_tail_all_workers,
                            show_error=show_error,
                        )

                    if ctx.json_output:
                        if wait_succeeded:
                            click.echo(json_formatter.format_json(safe_job_data, success=True))
                            sys.exit(EXIT_SUCCESS)
                        # A failed job is the canonical failure shape, and it used to leave as
                        # `{"success": false, "data": ...}` on stdout - a success-shaped pipe
                        # document carrying a failure. The error envelope goes to stderr (with the
                        # status and wait_diagnostics riding in `data`), stdout stays empty, and a
                        # `| jq` reader sees one success document or nothing.
                        _handle_error(
                            ctx,
                            "JobFailed",
                            f"Job {job_id} ended in terminal status {current_status}",
                            EXIT_GENERAL_ERROR,
                            hint=(
                                "this is the job's own verdict, not a client failure; the evidence "
                                "is in data.wait_diagnostics. `inspire job logs "
                                f"{job_id}` prints the full log"
                            ),
                            data=safe_job_data,
                        )

                    # Ends the spinner line, which lives on stderr.
                    click.echo("", err=True)
                    click.echo(human_formatter.format_job_status(safe_job_data))
                    diagnostics = safe_job_data.get("wait_diagnostics")
                    if isinstance(diagnostics, dict):
                        click.echo("", err=True)
                        click.echo(_format_wait_diagnostics(diagnostics), err=True)

                    if wait_succeeded:
                        sys.exit(EXIT_SUCCESS)
                    sys.exit(EXIT_GENERAL_ERROR)

            except Exception as e:
                if not ctx.json_output:
                    click.echo(f"\nWarning: Failed to get status: {e}", err=True)
                blind.record_failure(e)

            job_deps.time.sleep(interval)

    except BlindError as e:
        # Exit 13 with type `Blind`: the window this wait just covered has no observations in it, and
        # reading it as "quiet for 90 seconds" is the exact mistake this command is here to prevent.
        _handle_error(
            ctx,
            "Blind",
            f"{e.consecutive_failures} consecutive status reads failed ({e.last_error}); "
            f"the job has been unobservable since {_iso_from_epoch(e.last_ok_at)}",
            EXIT_API_ERROR,
            hint=(
                "the poll could not see the platform - treat the whole window as unobserved, not as "
                "quiet. Re-run once connectivity is back."
            ),
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nInterrupted", err=True)
        sys.exit(EXIT_GENERAL_ERROR)


@click.command("command")
@click.argument("job_id")
@json_option
@pass_context
def show_command(ctx: Context, job_id: str, json_output: bool = False) -> None:
    """Show the training command used for a job."""

    json_output = resolve_json_output(ctx, json_output)
    job_id = resolve_job_id(ctx, job_id)

    command_value = None
    source = None

    try:
        Config.from_files_and_env()

        result = browser_api_module.get_job_detail(job_id)
        job_data = result.get("data", {})
        command_value = job_data.get("command")
        if command_value:
            source = "api"
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if not command_value:
        _handle_error(
            ctx,
            "CommandNotFound",
            f"No command found for job {job_id}",
            EXIT_API_ERROR,
        )
        return

    if ctx.json_output:
        payload = {"job_id": job_id, "command": command_value}
        if source:
            payload["source"] = source
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo(command_value)


__all__ = [
    "events",
    "instances",
    "list_jobs",
    "metrics",
    "show_command",
    "status",
    "stop",
    "wait",
]
