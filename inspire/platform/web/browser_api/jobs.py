"""Browser (web-session) APIs for jobs and users.

The web UI exposes job listing endpoints (and related user listings) that are
not part of the OpenAPI surface. These endpoints require a web-session cookie.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Any, Optional, cast

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.browser_api.errors import (
    ensure_browser_api_success,
    get_browser_api_action_result,
)
from inspire.platform.web.browser_api.ws_exec import (
    PlatformWsUnavailableError,
    execute_platform_ws_command,
)
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

_CREATE_RATE_LIMIT_MAX_RETRIES = 7
_CREATE_RATE_LIMIT_DEFAULT_DELAY_SECONDS = 1.0
_CREATE_RATE_LIMIT_MAX_DELAY_SECONDS = 30.0

__all__ = [
    "JobInfo",
    "JobInstanceNotAttachableError",
    "JobReadinessTimeoutError",
    "JOB_STATUS_ALIASES",
    "JOB_TERMINAL_STATUSES",
    "NATIVE_JOB_STATUSES",
    "canonical_job_status",
    "is_job_terminal_status",
    "native_job_statuses_for_status",
    "create_training_job",
    "get_current_user",
    "get_job_detail",
    "get_train_resource_prices",
    "get_train_job_workdir",
    "get_job_events",
    "get_job_platform_logs",
    "get_job_resource_metrics",
    "get_project_quota_jobs",
    "get_user_quota_jobs",
    "list_job_events",
    "list_job_instances",
    "list_job_users",
    "list_jobs",
    "list_jobs_with_canonical_status",
    "list_task_dimensions",
    "execute_job_command",
    "stop_training_job",
]

# Canonical job-status vocabulary. The platform answers with `job_*` spellings, the console and the
# local cache keep the uppercase short forms, and the keep-alive window adds states that are already
# over but still hold the card. Every command that filters or polls on status resolves both sides of
# its comparison through this table, so the per-command alias sets that used to drift cannot drift
# again. Unknown spellings survive as their uppercase form, which keeps an unlisted platform status
# matchable by exact (case-insensitive) name instead of silently matching nothing.
JOB_STATUS_ALIASES: dict[str, str] = {
    "pending": "PENDING",
    "job_pending": "PENDING",
    "job_creating": "PENDING",
    "running": "RUNNING",
    "job_running": "RUNNING",
    "queuing": "QUEUING",
    "job_queuing": "QUEUING",
    "succeeded": "SUCCEEDED",
    "succeeded_retaining": "SUCCEEDED",
    "job_succeeded": "SUCCEEDED",
    "job_succeed_reserving": "SUCCEEDED",
    "job_success_reserving": "SUCCEEDED",
    "failed": "FAILED",
    "failed_retaining": "FAILED",
    "job_failed": "FAILED",
    "job_fail_reserving": "FAILED",
    # A create that never produced an attempt is over immediately; the platform can leave these in
    # ListJobs indefinitely with no finished_at, so treating the spelling as unknown made months-old
    # failures look active.
    "job_create_failed": "FAILED",
    "cancelled": "CANCELLED",
    "job_cancelled": "CANCELLED",
    "job_stopped": "CANCELLED",
    # The OpenAPI surface documents STOPPED (API_ENDPOINTS.md, job statuses) as the spelling a
    # stopped job answers with, while the console says CANCELLED; both mean the run is over.
    "stopped": "CANCELLED",
}

# Canonical states a poll loop may stop in. The keep-alive spellings collapse here too: the job has
# ended even while its reservation keeps the GPU warm for the rest of the retention window.
JOB_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


def canonical_job_status(status: Any) -> str:
    """Collapse any status spelling to the canonical token used for filtering and polling."""
    value = str(status if status is not None else "").strip()
    if not value:
        return ""
    return JOB_STATUS_ALIASES.get(value.lower(), value.upper())


def is_job_terminal_status(status: Any) -> bool:
    """True when this status means the job is over and no further change is expected."""
    return canonical_job_status(status) in JOB_TERMINAL_STATUSES


def _native_job_status_groups() -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = {}
    for spelling, canonical in JOB_STATUS_ALIASES.items():
        if spelling.startswith("job_"):
            groups.setdefault(canonical, []).append(spelling)
    return {canonical: tuple(spellings) for canonical, spellings in groups.items()}


# The alias table read backwards. ListJobs' own `status` filter answers only to the platform's
# spellings: a canonical token such as `RUNNING`, or the console's `STOPPED`, filters to nothing. So a
# caller holding one canonical state has to expand it into its `job_*` group and ask once per spelling.
# Derived from the table above rather than written out again, so a spelling the CLI can name is a
# spelling the CLI can filter on.
NATIVE_JOB_STATUSES: dict[str, tuple[str, ...]] = _native_job_status_groups()


def native_job_statuses_for_status(status: Any) -> tuple[str, ...]:
    """The platform `job_*` spellings that carry this status, in the order the table lists them.

    Empty for a token the table does not know, which is how a caller learns the server cannot filter on
    it: asking for a spelling the platform does not use returns nothing rather than an error.
    """
    return NATIVE_JOB_STATUSES.get(canonical_job_status(status), ())


def _creator_identity(data: dict[str, Any]) -> tuple[str, str]:
    """Read the creator's `(id, name)` from the list payload.

    `ListJobs` normally nests a dict, but it has also answered `created_by` as null, empty or a bare
    numeric id, and callers fall back to the flat `created_by_id`/`created_by_name` fields. Any other
    scalar (notably a bare string) is not treated as an id: a name is not ownership evidence, and an
    unknown creator has to stay visible rather than be filtered into a false "mine" or "theirs".
    """
    raw = data.get("created_by")
    if isinstance(raw, dict):
        creator_id = raw.get("id")
        creator_name = raw.get("name")
    elif isinstance(raw, int) and not isinstance(raw, bool):
        creator_id = raw
        creator_name = None
    else:
        creator_id = None
        creator_name = None
    creator_id = creator_id or data.get("created_by_id")
    creator_name = creator_name or data.get("created_by_name")
    return str(creator_id or "").strip(), str(creator_name or "").strip()


@dataclass
class JobInfo:
    """Training job information."""

    job_id: str
    name: str
    status: str
    command: str
    created_at: str
    finished_at: Optional[str]
    created_by_name: str
    created_by_id: str
    project_id: str
    project_name: str
    compute_group_name: str
    gpu_type: str
    gpu_count: int
    instance_count: int
    priority: int
    workspace_id: str
    priority_level: str = ""
    priority_name: str = ""
    auto_fault_tolerance: bool = False
    fault_tolerance_max_retry: int | None = None

    @classmethod
    def from_api_response(cls, data: dict) -> "JobInfo":
        framework_config = data.get("framework_config", [{}])[0]
        gpu_info = framework_config.get("instance_spec_price_info", {}).get("gpu_info", {})

        priority_name = str(data.get("priority_name") or "")
        priority_value = priority_name or data.get("task_priority") or data.get("priority") or 0
        try:
            priority = int(priority_value)
        except (TypeError, ValueError):
            priority = 0

        retry_value = data.get("fault_tolerance_max_retry")
        try:
            fault_tolerance_max_retry = int(retry_value) if retry_value is not None else None
        except (TypeError, ValueError):
            fault_tolerance_max_retry = None

        created_by_id, created_by_name = _creator_identity(data)

        return cls(
            job_id=data.get("job_id", ""),
            name=data.get("name", ""),
            status=data.get("status", ""),
            command=data.get("command", ""),
            created_at=data.get("created_at", ""),
            finished_at=data.get("finished_at"),
            created_by_name=created_by_name,
            created_by_id=created_by_id,
            project_id=data.get("project_id", ""),
            project_name=data.get("project_name", ""),
            compute_group_name=data.get("logic_compute_group_name", ""),
            gpu_type=gpu_info.get("gpu_type_display", ""),
            gpu_count=framework_config.get("gpu_count", 0),
            instance_count=framework_config.get("instance_count", 1),
            priority=priority,
            workspace_id=data.get("workspace_id", ""),
            priority_level=str(data.get("priority_level") or ""),
            priority_name=priority_name,
            auto_fault_tolerance=bool(data.get("auto_fault_tolerance", False)),
            fault_tolerance_max_retry=fault_tolerance_max_retry,
        )


def _train_referer(job_id: str | None = None) -> str:
    if job_id:
        return f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}"
    return f"{_get_base_url()}/jobs/distributedTraining"


def _legacy_envelope(result: dict[str, Any]) -> dict[str, Any]:
    """Keep the browser adapter's public return shape stable across v1 and v2."""
    return {"code": 0, "data": result}


def list_jobs(
    workspace_id: Optional[str] = None,
    created_by: Optional[str] = None,
    status: Optional[str] = None,
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[JobInfo], int]:
    """List training jobs using the browser API."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page_num": page_num,
        "page_size": page_size,
    }

    if created_by:
        body["created_by"] = created_by
    if status:
        body["status"] = status

    response = _request_json(
        session,
        "POST",
        "/api/v2/train?Action=ListJobs",
        referer=_train_referer(),
        body=body,
        timeout=30,
    )
    result = get_browser_api_action_result(response, action="List training jobs")
    jobs_data = result.get("jobs", [])
    total = result.get("total", 0)

    jobs = [JobInfo.from_api_response(j) for j in jobs_data]
    return jobs, total


def list_jobs_with_canonical_status(
    status: Optional[str] = None,
    workspace_id: Optional[str] = None,
    created_by: Optional[str] = None,
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[JobInfo], int]:
    """List the jobs sitting in one status, filtered by the server instead of by us.

    `status` takes any spelling and is expanded into the platform's own `job_*` group, asked for once
    per spelling: one call for `RUNNING`, at most three for the states that carry a keep-alive spelling.
    A token with no platform spelling is not pushed at all: the server answers a spelling it does not use
    with an empty page rather than an error, so that caller gets the unfiltered page and filters it here.
    """
    spellings = native_job_statuses_for_status(status)
    if not spellings:
        return list_jobs(
            workspace_id=workspace_id,
            created_by=created_by,
            page_num=page_num,
            page_size=page_size,
            session=session,
        )

    merged: dict[str, JobInfo] = {}
    total = 0
    for spelling in spellings:
        spelling_jobs, spelling_total = list_jobs(
            workspace_id=workspace_id,
            created_by=created_by,
            status=spelling,
            page_num=page_num,
            page_size=page_size,
            session=session,
        )
        total += int(spelling_total or 0)
        for job in spelling_jobs:
            merged.setdefault(job.job_id, job)

    # Each page arrives newest-first; across spellings the order has to be rebuilt.
    ordered = sorted(
        merged.values(),
        key=lambda job: _timestamp_seconds(job.created_at) or 0,
        reverse=True,
    )
    return ordered, total


def create_training_job(
    payload: dict[str, Any],
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Create a training job using the current web-session API."""
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/create"),
        referer=_train_referer(),
        body=payload,
        timeout=60,
        rate_limit_max_retries=_CREATE_RATE_LIMIT_MAX_RETRIES,
        rate_limit_default_delay_seconds=_CREATE_RATE_LIMIT_DEFAULT_DELAY_SECONDS,
        rate_limit_max_delay_seconds=_CREATE_RATE_LIMIT_MAX_DELAY_SECONDS,
    )
    if "code" in response:
        ensure_browser_api_success(response, action="Create training job")
        result = response.get("data", {})
        if not isinstance(result, dict):
            raise ValueError("Create training job failed: platform data is not an object")
    else:
        result = get_browser_api_action_result(response, action="Create training job")
    return _legacy_envelope(result)


def get_job_detail(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Get training job detail using the browser API."""
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        "/api/v2/train?Action=GetJob",
        referer=_train_referer(job_id),
        body={"job_id": job_id},
        timeout=30,
    )
    result = get_browser_api_action_result(response, action="Get training job detail")
    return _legacy_envelope(result)


def stop_training_job(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Stop a training job using the browser API."""
    if session is None:
        session = get_web_session()

    response = _request_json(
        session,
        "POST",
        "/api/v2/train?Action=StopJob",
        referer=_train_referer(job_id),
        body={"job_id": job_id},
        timeout=30,
    )
    result = get_browser_api_action_result(response, action="Stop training job")
    return _legacy_envelope(result)


def list_job_instances(
    job_id: str,
    *,
    page_num: int = 1,
    page_size: int = 100,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List platform instances/pods for a training job."""
    if session is None:
        session = get_web_session()

    body = {"page_num": page_num, "page_size": page_size, "jobId": job_id}
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/instance_list"),
        referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}",
        body=body,
        timeout=30,
    )
    ensure_browser_api_success(data, action="List training job instances")
    payload = data.get("data", {})
    return payload if isinstance(payload, dict) else {}


# Pod names are `<job-id>-worker-<attempt>-<worker-index>`: on a single-GPU job the trailing index
# stays 0 while the middle number is the fault-tolerance attempt, so `-worker-3-0` means
# "worker 0, attempt 4" - not a fourth node or rank. Verified against live 1xH100 jobs whose
# `running_round` equalled the middle number + 1 (issue #30).
_WORKER_ATTEMPT_TAIL_RE = re.compile(r"-worker-(\d+)-(\d+)$")
_WORKER_ONLY_TAIL_RE = re.compile(r"-worker-(\d+)$")


def _job_instance_indices(instance_name: str) -> tuple[int, int] | None:
    """Return ``(attempt, worker_index)`` parsed from a pod name, or None if unrecognised."""
    match = _WORKER_ATTEMPT_TAIL_RE.search(instance_name)
    if match:
        return int(match.group(1)), int(match.group(2))
    # Tolerate the older single-segment shape by treating it as the first attempt.
    match = _WORKER_ONLY_TAIL_RE.search(instance_name)
    if match:
        return 0, int(match.group(1))
    return None


def _job_worker_index(instance_name: str) -> int | None:
    parsed = _job_instance_indices(instance_name)
    return None if parsed is None else parsed[1]


def _job_attempt_index(instance_name: str) -> int | None:
    parsed = _job_instance_indices(instance_name)
    return None if parsed is None else parsed[0]


def _instance_label(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "")
    node = str(item.get("node") or "") or "never placed"
    status = str(item.get("instance_status") or "") or "unknown"
    return f"{name} [{status} on {node}]"


# Placeholder spellings for "no node was ever chosen". Whether a field is empty or filled with a
# dash decides between "change the resource request" and "read the logs of a dead pod", so all of
# them collapse to the same answer.
_NODE_PLACEHOLDERS = {"", "-", "--", "none", "null", "n/a", "nan", "unknown"}


def _normalize_node(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return "" if text.lower() in _NODE_PLACEHOLDERS else text


def _instance_class(item: dict[str, Any]) -> str:
    """Classify one attempt so that opposite remedies are never counted together.

    ``never_placed`` (no node, never started) means the request was not admitted anywhere, so the
    action is to change the resource request or pool. ``failed_on_node`` means it ran and died, so
    the action is to read that attempt's logs or consider the node. Folding the two into one
    "failed" count sends people to retry a request that can never be scheduled (issue #31).
    """
    status = str(item.get("instance_status") or "").lower()
    node = _normalize_node(item.get("node"))
    started = str(item.get("started_at") or "").strip()
    finished = str(item.get("finished_at") or "").strip()

    if "creating" in status or "pending" in status or "waiting" in status:
        return "creating"
    if "running" in status:
        return "running"
    if "reserving" in status:
        # `instance_fail_reserving` is the failed-but-still-holding-the-card state, reachable only
        # while the keep-alive window is open. Folding it into plain "failed" hides the one round
        # where `job stop` still buys a GPU back.
        return "reserving"
    if not node and not started:
        return "never_placed"
    if "fail" in status or "error" in status:
        return "failed_on_node" if node else "never_placed"
    if "succe" in status or "finish" in status or "complete" in status:
        return "succeeded" if node else "finished_no_node"
    del finished  # only used to distinguish terminal states above
    return "other"


def describe_instance(item: dict[str, Any]) -> dict[str, Any]:
    """Normalised view of one attempt, for announcements and attempt listings.

    ``phase_since`` and ``phase_age_seconds`` appear only on an attempt that is still live (no
    ``finished_at``) and reports a usable ``started_at``; anything else leaves both keys absent, because an
    invented zero reads as "just changed phase". Both are measured from ``started_at``, which the platform
    sets when the attempt is **scheduled** - the earliest evidence we hold of its current phase, not the
    phase's own start. So the number is time-since-scheduling: an attempt running for 30s after ten minutes
    of image pull reports ~10m30s. Say "since scheduling", never "time in phase". A terminal attempt keeps
    ``duration_ms`` and gets no phase age - its phase is already over.
    """
    name = str(item.get("name") or "")
    indices = _job_instance_indices(name)
    started = str(item.get("started_at") or "").strip()
    finished = str(item.get("finished_at") or "").strip()
    node = _normalize_node(item.get("node"))
    duration_ms: int | None = None
    if started.isdigit() and finished.isdigit():
        # Measured from `started_at`, which the platform sets when the attempt is **scheduled**, not when
        # its container starts: an attempt whose pod never left creation produced a 543s "duration".
        # Renderers must therefore say "after scheduling", never "ran for".
        duration_ms = max(0, (int(finished) - int(started)) // 1000) * 1000
    described: dict[str, Any] = {
        "name": name,
        "attempt": None if indices is None else indices[0],
        "worker_index": None if indices is None else indices[1],
        "status": str(item.get("instance_status") or ""),
        "node": node or None,
        "kind": _instance_class(item),
        "duration_ms": duration_ms,
    }
    if not finished:
        # `0` is the platform's "never set", not an epoch: a 1970 phase start would be a fabricated claim.
        started_seconds = _timestamp_seconds(started) if started not in ("", "0") else None
        if started_seconds is not None:
            described["phase_since"] = datetime.fromtimestamp(
                started_seconds, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            described["phase_age_seconds"] = max(0, int(time.time()) - started_seconds)
    return described


def _reserve_window_ms(value: Any) -> int | None:
    """Milliseconds a keep-alive window lasts, or None when the record does not state one.

    The submit paths write `reserve_on_fail_ms`/`reserve_on_success_ms` as stringified milliseconds
    and nothing on this side ever read them back, so an absent, unparseable, or non-positive value is
    reported as unknown. A deadline nobody configured is not a deadline.
    """
    if value in (None, ""):
        return None
    try:
        numeric = float(str(value).strip())
    except ValueError:
        return None
    return int(numeric) if numeric > 0 else None


def _reserve_window_kind(status: Any) -> str | None:
    """Which keep-alive window a reserving round is running on, or None when the status cannot say."""
    text = str(status or "").lower()
    if "fail" in text or "error" in text:
        return "fail"
    if "succe" in text:
        return "success"
    return None


def summarize_attempts(
    instances: list[dict[str, Any]],
    *,
    reserve_on_fail_ms: Any = None,
    reserve_on_success_ms: Any = None,
) -> dict[str, Any]:
    """Count attempts by class and derive the node judgement a human keeps getting wrong.

    Node names differ only in their trailing digits, so asking a person to compare them invites the
    wrong answer (a 12-character clip collapsed distinct machines during the investigation that
    produced this). Derive the statement instead.

    Pass the job's keep-alive windows to also get the release time of each reserving round: the round
    carries `finished_at`, `reserve_window`, `reserve_window_ms`, and `reserve_until` (all `null`
    where the platform withheld a piece), and the summary carries `reserving_until` - the moment the
    **last** held card is given back. `reserving_until` is only non-null when every reserving round's
    deadline is computable, because a claim about the whole set has to hold for its last member.
    """
    described = [describe_instance(item) for item in instances]
    counts: dict[str, int] = {}
    for entry in described:
        counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
    ran_nodes = [str(entry["node"]) for entry in described if entry["node"]]
    repeated_nodes = sorted({node for node in ran_nodes if ran_nodes.count(node) > 1})
    summary: dict[str, Any] = {
        "total": len(described),
        "counts": counts,
        "failed_on_node": counts.get("failed_on_node", 0),
        "never_placed": counts.get("never_placed", 0),
        "distinct_nodes": len(set(ran_nodes)),
        "repeated_nodes": repeated_nodes,
        "attempts": described,
        "reserving_until": _annotate_reserve_deadlines(
            instances,
            described,
            {
                "fail": _reserve_window_ms(reserve_on_fail_ms),
                "success": _reserve_window_ms(reserve_on_success_ms),
            },
        ),
    }
    return summary


def _annotate_reserve_deadlines(
    instances: list[dict[str, Any]],
    described: list[dict[str, Any]],
    windows: dict[str, int | None],
) -> str | None:
    """Fill in each reserving round's deadline and return the last one, or None if any is unknown."""
    deadlines: list[int] = []
    seen = 0
    for item, entry in zip(instances, described, strict=True):
        if entry["kind"] != "reserving":
            continue
        seen += 1
        kind = _reserve_window_kind(entry.get("status"))
        window_ms = windows.get(kind) if kind else None
        finished_raw = str(item.get("finished_at") or "").strip()
        finished_seconds = (
            _timestamp_seconds(finished_raw) if finished_raw not in ("", "0") else None
        )
        entry["reserve_window"] = kind
        entry["reserve_window_ms"] = window_ms
        entry["finished_at"] = (
            None
            if finished_seconds is None
            else datetime.fromtimestamp(finished_seconds, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        )
        if finished_seconds is None or window_ms is None:
            entry["reserve_until"] = None
            continue
        deadline = finished_seconds + window_ms // 1000
        entry["reserve_until"] = datetime.fromtimestamp(deadline, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        deadlines.append(deadline)
    if not seen or len(deadlines) != seen:
        return None
    return datetime.fromtimestamp(max(deadlines), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_job_instance(
    instances: list[dict[str, Any]],
    *,
    worker: int,
    instance_name: str | None,
    attempt: int | None = None,
) -> dict[str, Any]:
    """Pick one instance for a job.

    Several instances are the *same* worker - one per fault-tolerance attempt - so "worker 0" is
    ambiguous by construction once a job has retried. The default is the **latest attempt**, because
    that is the one either running now or most recently failed; picking it must not fall back to
    string order, where ``worker-10-0`` sorts before ``worker-2-0``.
    """
    if instance_name:
        for item in instances:
            if item.get("name") == instance_name:
                return item
        available = ", ".join(str(item.get("name") or "") for item in instances)
        raise ValueError(f"Instance {instance_name!r} not found. Available instances: {available}")

    candidates: list[tuple[int, dict[str, Any]]] = []
    for item in instances:
        parsed = _job_instance_indices(str(item.get("name") or ""))
        if parsed is None or parsed[1] != worker:
            continue
        candidates.append((parsed[0], item))

    if candidates:
        if attempt is not None:
            for attempt_index, item in candidates:
                if attempt_index == attempt:
                    return item
            available_attempts_text = ", ".join(
                str(attempt_index) for attempt_index, _ in sorted(candidates)
            )
            raise ValueError(
                f"Attempt {attempt} not found for worker {worker}. "
                f"Available attempts: {available_attempts_text}"
            )
        return max(candidates, key=lambda pair: pair[0])[1]

    if worker == 0 and len(instances) == 1:
        return instances[0]

    available_attempts = sorted(
        {
            attempt_value
            for item in instances
            if (attempt_value := _job_attempt_index(str(item.get("name") or ""))) is not None
        }
    )
    available_workers = sorted(
        worker_id
        for worker_id in (_job_worker_index(str(item.get("name") or "")) for item in instances)
        if worker_id is not None
    )
    if available_workers:
        raise ValueError(
            f"Worker {worker} not found. Available workers: "
            f"{', '.join(str(item) for item in available_workers)}. "
            f"Observed attempts: {', '.join(str(item) for item in available_attempts) or 'none'}"
        )
    available = ", ".join(_instance_label(item) for item in instances)
    raise ValueError(f"No worker-indexed instances found. Available instances: {available}")


def _timestamp_seconds(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return int(numeric / 1000) if numeric > 10_000_000_000 else int(numeric)
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
        return int(numeric / 1000) if numeric > 10_000_000_000 else int(numeric)
    except ValueError:
        pass
    try:
        normalized = text.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return None


def _job_log_time_range(job_data: dict[str, Any]) -> tuple[int | str, int | str]:
    created = job_data.get("created_at") or "0"
    finished = job_data.get("finished_at")
    if finished:
        return created, finished
    return created, str(int(time.time() * 1000) + 60_000)


def get_job_platform_logs(
    job_id: str,
    *,
    page_size: int = 100,
    worker: int = 0,
    instance_name: str | None = None,
    search_after: list[str] | None = None,
    sort: str = "descend",
    attempt: int | None = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch platform logs for one job instance (train?Action=GetJobLog, #82)."""
    if session is None:
        session = get_web_session()

    detail = get_job_detail(job_id, session=session).get("data", {})
    instances_payload = list_job_instances(job_id, page_size=-1, session=session)
    instances = instances_payload.get("items", [])
    if not isinstance(instances, list) or not instances:
        raise ValueError(f"No instances found for job {job_id}")
    selected = _select_job_instance(
        [item for item in instances if isinstance(item, dict)],
        worker=worker,
        instance_name=instance_name,
        attempt=attempt,
    )
    selected_name = str(selected.get("name") or "")
    if not selected_name:
        raise ValueError(f"Selected instance for job {job_id} has no name")

    start_timestamp_ms, end_timestamp_ms = _job_log_time_range(detail)
    body: dict[str, Any] = {
        "page_size": page_size,
        "filter": {
            "podNames": [selected_name],
            "start_timestamp_ms": start_timestamp_ms,
            "end_timestamp_ms": end_timestamp_ms,
        },
        "sorter": [
            {"field": "time", "sort": sort},
            {"field": "log-id.keyword", "sort": sort},
        ],
    }
    if search_after:
        body["search_after"] = search_after

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/logs/train"),
        referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}",
        body=body,
        timeout=60,
    )
    ensure_browser_api_success(data, action="Get training job logs")
    payload = data.get("data", {})
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("instance", selected)
    payload.setdefault("instance_name", selected_name)
    payload.setdefault("selected", describe_instance(selected))
    payload.setdefault(
        "attempt", None if attempt is None else describe_instance(selected)["attempt"]
    )
    payload.setdefault("job_id", job_id)
    return payload


class JobReadinessTimeoutError(RuntimeError):
    """No executable instance of the job appeared within the readiness budget (#50).

    Distinct from a command timeout on purpose: here nothing was ever sent to the pod, so the caller's command
    did not run, let alone overrun. Callers map this to their timeout exit while a command that overran its own
    budget keeps its own exit, because an agent that cannot tell the two apart cannot tell "keep waiting" from
    "the command is still producing output somewhere".
    """


class JobInstanceNotAttachableError(RuntimeError):
    """The instance exists but cannot host a PTY, so the command was not sent.

    The platform's own record - a terminal ``instance_status`` together with a ``finished_at`` - is what
    says this; it is never inferred from a socket error, because "the channel broke" and "the pod is gone"
    produce byte-identical output and only the pod's record tells them apart. Opening the socket anyway
    bought a ``Connection to remote host was lost`` that reads exactly like an unstable channel, on a pod
    that stopped hours ago (LESSONS-ARCHIVE rule 11b).
    """


#: Instance classifications that mean "wait, this may still come up". A pod listed before it is attachable
#: is a known platform behaviour (#50), so with a readiness budget to spend this is a state to wait in.
_PENDING_INSTANCE_KINDS = frozenset({"creating"})

#: Instance classifications that mean "this pod is over", accepted only when ``finished_at`` is also set -
#: the platform's own witness, so an unfamiliar status string alone can never block a working exec.
_TERMINAL_INSTANCE_KINDS = frozenset(
    {"failed_on_node", "never_placed", "succeeded", "finished_no_node"}
)


def _attachability(instance: dict[str, Any]) -> tuple[str, str]:
    """``(kind, detail)`` for one selected instance: is a PTY attachable to it, and what says so?"""
    kind = _instance_class(instance)
    finished = str(instance.get("finished_at") or "").strip()
    status = str(instance.get("instance_status") or "").strip() or "unknown"
    detail = (
        f"instance_status={status}, node={_normalize_node(instance.get('node')) or 'never placed'}"
    )
    if finished not in ("", "0") and kind in _TERMINAL_INSTANCE_KINDS:
        return "finished", f"{detail}, finished_at={finished}"
    if kind in _PENDING_INSTANCE_KINDS:
        return "pending", detail
    return "attachable", detail


def _annotate_with_instance(
    error: PlatformWsUnavailableError, instance: dict[str, Any]
) -> PlatformWsUnavailableError:
    """Name the instance the socket was opened against, on the error that says it did not work.

    ``Connection to remote host was lost`` on its own cannot be triaged after the fact: whether the pod
    was running, reserving or already created-but-not-started decides between blaming the gateway, the
    node and the resource request. The instance record was already in hand when the socket was opened, so
    the failure carries it. Only `PlatformWsUnavailableError` goes through here - it is the one error type
    whose whole meaning is "this instance gave us no channel", and rebuilding an arbitrary exception from
    its text would lose whatever else it carried.
    """
    kind, detail = _attachability(instance)
    return PlatformWsUnavailableError(
        f"{error} [instance {_instance_label(instance)}; attachability={kind}; {detail}]"
    )


# How often the readiness wait re-reads the instance list. Matches the coarse polling granularity the rest of
# the CLI uses (`job wait --interval` defaults to 30s, but this gates an interactive command), and it is long
# enough that a `--wait-timeout 300` cannot turn into 300 instance-list requests.
_READINESS_POLL_INTERVAL_SECONDS = 5.0


def _resolve_executable_job_instance(
    job_id: str,
    *,
    worker: int = 0,
    instance_name: str | None = None,
    attempt: int | None = None,
    wait_timeout: int = 0,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Find the instance a command should run in, optionally waiting for one to appear (#50).

    The platform answers the instance list before a pod is attachable, and before that it answers with no rows
    at all, so a caller that opens the socket immediately is reporting "job not running" about a job that was
    still being scheduled. `wait_timeout` > 0 turns that answer into a state worth waiting in: the list is
    re-read every ``_READINESS_POLL_INTERVAL_SECONDS`` until a selection succeeds or the budget is spent.

    Both a *lookup* failure (no rows yet) and a *selection* failure (the rows do not name what was asked for,
    e.g. the pod for ``--worker 1`` has not been created yet) are readiness states, so both are retried and
    both are reported. So is a row that names a pod which is still being created, once there is a budget to
    wait in: the platform lists a pod before it can be attached to, and "creating" is the platform saying so.
    Anything else - a transport or auth failure - propagates immediately; waiting on it would only hide it.

    A row whose own record says the pod is over (terminal status *and* ``finished_at``) is never a readiness
    state and never a socket: it raises `JobInstanceNotAttachableError` rather than opening a channel that
    would close at once and be read as an unstable exec endpoint.

    With the default `wait_timeout` of 0 the first answer is the answer, which is exactly what this function
    replaced: apart from that terminal case, the raised errors, including their text, are unchanged, so
    callers that never pass the new argument see no difference at all.
    """
    if instance_name and worker != 0:
        raise ValueError("--instance and --worker are mutually exclusive")

    deadline = None if wait_timeout <= 0 else time.time() + float(wait_timeout)
    last_error: Optional[ValueError] = None
    while True:
        try:
            instances_payload = list_job_instances(job_id, page_size=-1, session=session)
            instances = instances_payload.get("items", [])
            if not isinstance(instances, list) or not instances:
                raise ValueError(f"No instances found for job {job_id}")
            selected = _select_job_instance(
                [item for item in instances if isinstance(item, dict)],
                worker=worker,
                instance_name=instance_name,
                attempt=attempt,
            )
            selected_name = str(selected.get("name") or "")
            if not selected_name:
                raise ValueError(f"Selected instance for job {job_id} has no name")
            kind, detail = _attachability(selected)
            if kind == "finished":
                # Opening the socket here is what used to produce the most misleading failure this
                # channel has: a pod that finished hours ago answers the upgrade with a closed channel,
                # which reads exactly like an unstable exec endpoint on a live job.
                raise JobInstanceNotAttachableError(
                    f"Instance {selected_name} cannot host a command: {detail}. "
                    f"The newest attempt of job {job_id} is over, so nothing was sent."
                )
            if kind == "pending" and deadline is not None:
                # A pod the instance list names before it is attachable is a readiness state, which is
                # what --wait-timeout buys (#50). With no budget to spend the answer stays whatever the
                # socket says, so the default path keeps exactly the behaviour it had before.
                raise ValueError(
                    f"Selected instance {selected_name} is not attachable yet: {detail}"
                )
            return selected
        except ValueError as exc:
            last_error = exc
        if last_error is None:  # pragma: no cover - the body returns or records an error
            raise RuntimeError(f"Instance lookup for job {job_id} made no progress")
        if deadline is None:
            raise last_error
        remaining = deadline - time.time()
        if remaining <= 0:
            # The last answer is carried along because it is the useful half: after a typo'd --worker the
            # "Available workers: 0" list is what the caller has to act on, and after an empty list there is
            # simply nothing to name yet.
            raise JobReadinessTimeoutError(
                f"Readiness wait expired after {wait_timeout}s: {last_error}"
            )
        time.sleep(min(_READINESS_POLL_INTERVAL_SECONDS, remaining))


def execute_job_command(
    job_id: str,
    command: str,
    *,
    worker: int = 0,
    instance_name: str | None = None,
    timeout: float | None = 60.0,
    attempt: int | None = None,
    session: Optional[WebSession] = None,
    wait_timeout: int = 0,
) -> dict[str, Any]:
    """Execute one command in a running training job instance via PTY WebSocket.

    `timeout` bounds the command only. Opening the socket keeps the transport default handshake budget (see
    `ws_exec.execute_platform_ws_command`), and `wait_timeout` > 0 adds a bounded wait for an instance to exist
    before any socket is opened (#50).
    """
    if session is None:
        session = get_web_session()

    selected = _resolve_executable_job_instance(
        job_id,
        worker=worker,
        instance_name=instance_name,
        attempt=attempt,
        wait_timeout=wait_timeout,
        session=session,
    )
    selected_name = str(selected.get("name") or "")

    try:
        result = execute_platform_ws_command(
            endpoint_path="/train_job/remote_cmd",
            query={"job_id": job_id, "instance_name": selected_name},
            instance_name=selected_name,
            command=command,
            timeout=timeout,
            session=session,
        )
    except PlatformWsUnavailableError as exc:
        # Nothing was sent, and the instance record explains what did answer.
        raise _annotate_with_instance(exc, selected) from exc
    result["job_id"] = job_id
    result["instance"] = selected
    return result


def open_job_command_session(
    job_id: str,
    *,
    worker: int = 0,
    instance_name: str | None = None,
    attempt: int | None = None,
    session: Optional[WebSession] = None,
    wait_timeout: int = 0,
) -> Any:
    """Open one PTY session against a running job instance, for callers issuing many commands.

    `execute_job_command` is one instance lookup plus one WebSocket establishment per call. On a pod measured
    this morning as losing per-command sessions repeatedly - while a single session carried 83 commands without
    a single drop - that pricing makes multi-step operations (chunked upload, upload plus verification) fail by
    construction. Use this instead when the operation needs more than one command. `wait_timeout` behaves as in
    `execute_job_command`.
    """
    from inspire.platform.web.browser_api.ws_exec import PlatformWsSession

    if session is None:
        session = get_web_session()

    selected = _resolve_executable_job_instance(
        job_id,
        worker=worker,
        instance_name=instance_name,
        attempt=attempt,
        wait_timeout=wait_timeout,
        session=session,
    )
    selected_name = str(selected.get("name") or "")

    try:
        handle = PlatformWsSession.open(
            endpoint_path="/train_job/remote_cmd",
            query={"job_id": job_id, "instance_name": selected_name},
            instance_name=selected_name,
            session=session,
        )
    except PlatformWsUnavailableError as exc:
        raise _annotate_with_instance(exc, selected) from exc
    handle.instance_record = selected  # type: ignore[attr-defined]
    return handle


def get_job_events(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch scheduler/Kubernetes events for a training job."""
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/job_event_list"),
        referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}",
        body={"jobId": job_id},
        timeout=30,
    )
    ensure_browser_api_success(data, action="List training job events")
    payload = data.get("data", {})
    return payload if isinstance(payload, dict) else {}


def list_task_dimensions(
    *,
    workspace_id: str,
    page_num: int = 1,
    page_size: int = 100,
    project_id: str | None = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List task dimensions used by the platform metrics surface."""
    if session is None:
        session = get_web_session()

    filter_params: dict[str, Any] = {"workspace_id": workspace_id}
    if project_id:
        filter_params["project_id"] = project_id
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/cluster_metric/list_task_dimension"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body={"page_num": page_num, "page_size": page_size, "filter": filter_params},
        timeout=30,
    )
    ensure_browser_api_success(data, action="List task dimensions")
    payload = data.get("data", {})
    return payload if isinstance(payload, dict) else {}


def get_job_resource_metrics(
    job_id: str,
    *,
    metric_types: list[str] | None = None,
    interval_seconds: int = 300,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch time-series metrics for a training job."""
    if session is None:
        session = get_web_session()

    detail = get_job_detail(job_id, session=session).get("data", {})
    workspace_id = str(detail.get("workspace_id") or "")
    logic_compute_group_id = str(detail.get("logic_compute_group_id") or "")
    if not workspace_id:
        raise ValueError(f"Job {job_id} detail did not include workspace_id")
    if not logic_compute_group_id:
        raise ValueError(f"Job {job_id} detail did not include logic_compute_group_id")

    start = _timestamp_seconds(detail.get("created_at")) or 0
    end = (
        _timestamp_seconds(detail.get("finished_at"))
        or _timestamp_seconds(detail.get("updated_at"))
        or int(time.time())
    )
    if end < start:
        end = start
    selected_metric_types = metric_types or ["gpu_usage_rate", "gpu_memory_usage_rate"]
    metrics: dict[str, Any] = {}

    for metric_type in selected_metric_types:
        body = {
            "metric_types": [metric_type],
            "filter": {
                "logic_compute_group_id": logic_compute_group_id,
                "task_type": "distributed_training",
                "task_id": job_id,
            },
            "time_range": {
                "start_timestamp": start,
                "end_timestamp": end,
                "interval_second": interval_seconds,
            },
        }
        data = _request_json(
            session,
            "POST",
            _browser_api_path("/cluster_metric/resource_metric_by_time"),
            referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}",
            body=body,
            timeout=30,
        )
        ensure_browser_api_success(data, action="Get training job metrics")
        metrics[metric_type] = data.get("data", {})

    return {
        "job_id": job_id,
        "workspace_id": workspace_id,
        "logic_compute_group_id": logic_compute_group_id,
        "task_type": "distributed_training",
        "interval_seconds": interval_seconds,
        "start": start,
        "end": end,
        "metrics": metrics,
    }


def get_train_resource_prices(
    *,
    workspace_id: str,
    logic_compute_group_id: str,
    session: Optional[WebSession] = None,
) -> list[dict[str, Any]]:
    """Fetch train resource price entries for one logic compute group."""
    if session is None:
        session = get_web_session()

    body = {
        "workspace_id": workspace_id,
        "schedule_config_type": "SCHEDULE_CONFIG_TYPE_TRAIN",
        "logic_compute_group_id": logic_compute_group_id,
    }
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/resource_prices/logic_compute_groups/"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )
    ensure_browser_api_success(data, action="List training resource prices")

    payload = data.get("data", [])
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    return cast(
        list[dict[str, Any]],
        payload.get(
            "lcg_resource_spec_prices",
            payload.get("resource_spec_prices", payload.get("list", [])),
        ),
    )


def get_current_user(session: Optional[WebSession] = None) -> dict:
    """Get current user details."""
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "GET",
        _browser_api_path("/user/detail"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        timeout=30,
    )
    ensure_browser_api_success(data, action="Get current user")
    return data.get("data", {})


def get_user_quota_jobs(
    *,
    workspace_id: str,
    user_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Fetch the quota a single user is consuming, plus the jobs consuming it.

    This Action takes PascalCase body keys, unlike the snake_case body of ``ListJobs`` on the
    neighbouring ``/api/v2/train`` surface. The platform answers with ``{"quota": {...},
    "used_quota_jobs": [...]}``; a limit of ``-1`` is its spelling of "no cap", which callers render
    as unlimited rather than as a negative number.
    """
    if session is None:
        session = get_web_session()

    body = {"WorkspaceId": workspace_id, "UserId": user_id}
    response = _request_json(
        session,
        "POST",
        "/api/v2/job?Action=GetUserQuotaJobs",
        referer=_train_referer(),
        body=body,
        timeout=30,
    )
    return get_browser_api_action_result(response, action="Get user quota usage")


def get_project_quota_jobs(
    *,
    workspace_id: str,
    project_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Fetch the quota a single project is consuming, plus the jobs consuming it.

    Same shape as :func:`get_user_quota_jobs`, and it counts every workload in the project -
    training jobs, notebooks, and tensorboards alike - not only training jobs.
    """
    if session is None:
        session = get_web_session()

    body = {"WorkspaceId": workspace_id, "ProjectId": project_id}
    response = _request_json(
        session,
        "POST",
        "/api/v2/job?Action=GetProjectQuotaJobs",
        referer=_train_referer(),
        body=body,
        timeout=30,
    )
    return get_browser_api_action_result(response, action="Get project quota usage")


def list_job_users(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List users who have created jobs."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/users"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body={"workspace_id": workspace_id},
        timeout=30,
    )
    ensure_browser_api_success(data, action="List training job users")
    return data.get("data", {}).get("items", [])


def get_train_job_workdir(
    *,
    project_id: str,
    workspace_id: str,
    session: Optional[WebSession] = None,
) -> str | None:
    """Fetch the training job workdir for a project within a workspace."""
    if session is None:
        session = get_web_session()

    project_id = str(project_id or "").strip()
    workspace_id = str(workspace_id or "").strip()
    if not project_id or not workspace_id:
        raise ValueError("project_id and workspace_id are required")

    body = {
        "project_id": project_id,
        "workspace_id": workspace_id,
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/workdir"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )

    ensure_browser_api_success(data, action="Get training job workdir")

    payload = data.get("data")
    if isinstance(payload, str):
        value = payload.strip()
        return value or None

    return None


def list_job_events(
    job_id: str,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List K8s events for a training job. Best-effort: returns [] on any error."""
    try:
        events = get_job_events(job_id, session=session).get("events", [])
        if not isinstance(events, list):
            return []
        return events
    except Exception:
        return []
