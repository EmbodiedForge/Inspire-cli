"""Job logs command.

Logs are read from the platform log API (`/logs/train`), which serves what the job's container wrote to
stdout/stderr. There is no local log cache and no SSH/Gitea retrieval path.
"""

from __future__ import annotations

from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.platform.web import browser_api as browser_api_module


def _log_entry_text(entry: dict) -> str:
    for key in ("log", "message", "content", "text"):
        value = entry.get(key)
        if value not in (None, ""):
            return str(value)
    return str(entry)


def _attempt_summary_line(summary: dict) -> str:
    # One renderer for both `job logs` and `job status`; two copies of this line would drift, and the
    # whole point of #31 was that a reader could not see the retry accounting where they looked most.
    return human_formatter.format_attempt_summary(summary)


def _payload_logs(payload: Any) -> list[Any]:
    logs = payload.get("logs", []) if isinstance(payload, dict) else []
    return logs if isinstance(logs, list) else []


def _logs_list_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Canonical list shape (`items`/`total`) plus the attempt metadata (`selected`, `instance_name`).

    The platform answers with a `logs` list; every other list-ish command in the CLI answers
    `{"items": [...], "total": N}`, and an agent should not need a second parser for log reads.
    """
    data = dict(payload) if isinstance(payload, dict) else {}
    logs = data.pop("logs", [])
    items = logs if isinstance(logs, list) else []
    data["items"] = items
    data["total"] = len(items)
    return data


def _describe_job_attempts(ctx: Context, *, job_id: str) -> None:
    """List every attempt of a job, classified, so "which attempt am I looking at" is answerable."""
    try:
        items = browser_api_module.list_job_instances(job_id, page_size=-1).get("items") or []
        summary = browser_api_module.summarize_attempts([i for i in items if isinstance(i, dict)])
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(json_formatter.format_json(summary))
        return

    click.echo(_attempt_summary_line(summary))
    for entry in summary.get("attempts") or []:
        node = entry.get("node") or "never placed"
        duration = entry.get("duration_ms")
        # `started_at` is set when the attempt is *scheduled*, not when its container starts: a fixture
        # that never left pod creation was reported as "failed_on_node after 543s" by the previous
        # wording. Say what the interval is measured from.
        duration_text = f"  {duration // 1000}s after scheduling" if duration else ""
        click.echo(
            f"  attempt {entry.get('attempt')}: worker {entry.get('worker_index')} "
            f"[{entry.get('kind')}] {node}{duration_text}  ({entry.get('name')})"
        )


def _run_platform_job_logs(
    ctx: Context,
    *,
    job_id: str,
    tail: int | None,
    head: int | None,
    worker: int,
    instance_name: str | None,
    attempt: int | None = None,
) -> None:
    if tail is not None and head is not None:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--tail and --head cannot be used together",
            EXIT_VALIDATION_ERROR,
        )
        return

    # A requested 0 means "no lines", not "unset". The log API has no zero-size page, so a 0 limit
    # asks for the smallest page and the line is dropped here; the fetch still happens, so the
    # attempt selection, the JSON envelope and "No instances found" answer as they do for --tail 1.
    limit = head if head is not None else tail
    if limit is None:
        page_size, sort = 100, "descend"
    else:
        page_size = 1 if limit == 0 else limit
        sort = "ascend" if head is not None else "descend"

    try:
        payload = browser_api_module.get_job_platform_logs(
            job_id,
            page_size=page_size,
            worker=worker,
            instance_name=instance_name,
            sort=sort,
            attempt=attempt,
        )
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    fetched_logs = _payload_logs(payload)
    # A negative limit is passed through to the API untouched, as before, so it must not slice here.
    display_logs = fetched_logs[:limit] if (limit is not None and limit >= 0) else fetched_logs

    if ctx.json_output:
        json_payload = dict(payload) if isinstance(payload, dict) else {}
        json_payload["logs"] = display_logs
        click.echo(json_formatter.format_json(_logs_list_payload(json_payload)))
        return

    # Several instances are the same worker, one per fault-tolerance attempt. Reading the wrong
    # attempt's log is worse than reading none, so the selection is stated whenever it was implicit.
    selected = payload.get("selected") if isinstance(payload, dict) else None
    if isinstance(selected, dict) and not instance_name and attempt is None:
        node = selected.get("node") or "never placed"
        click.echo(
            f"logs: worker {worker}, attempt {selected.get('attempt')} "
            f"[{selected.get('kind')}] on {node} - newest attempt; "
            "use --attempt N or --attempts for the others",
            err=True,
        )

    if not display_logs:
        if fetched_logs:
            # The caller asked for fewer lines than exist (`--tail 0`). That is not the same claim as
            # "this attempt produced no logs", so print nothing rather than contradict them.
            return
        if isinstance(selected, dict) and selected.get("kind") == "never_placed":
            hint = ""
            try:
                items = browser_api_module.list_job_instances(job_id, page_size=-1).get("items")
                described = browser_api_module.summarize_attempts(
                    [item for item in (items or []) if isinstance(item, dict)]
                )["attempts"]
                ran = [
                    entry
                    for entry in described
                    if entry.get("attempt") is not None
                    and entry.get("kind") not in ("never_placed", "creating")
                ]
                if ran:
                    newest = max(ran, key=lambda entry: entry["attempt"])
                    hint = (
                        f" The newest attempt that actually ran is attempt {newest['attempt']} "
                        f"(`--attempt {newest['attempt']}`)."
                    )
            except Exception:  # noqa: BLE001 - a hint must not break the answer
                pass
            click.echo(
                f"Attempt {selected.get('attempt')} never started on any node, so it produced no "
                "logs. That is not a silent crash: the cause is in `inspire job events`, typically "
                "Unschedulable. Fix the resource request or pool rather than retrying it." + hint
            )
            return
        click.echo(f"No platform logs found for {job_id}.")
        return
    display_logs = list(reversed(display_logs)) if sort == "descend" else display_logs
    for entry in display_logs:
        if isinstance(entry, dict):
            click.echo(_log_entry_text(entry))
        else:
            click.echo(str(entry))


@click.command("logs")
@click.argument("job_id", required=False)
@click.option("--tail", "-n", type=int, help="Show last N lines only (0 shows none)")
@click.option("--head", type=int, help="Show first N lines only (0 shows none)")
@click.option("--worker", type=int, default=0, show_default=True, help="Platform worker index.")
@click.option(
    "--attempt",
    type=int,
    help="Fault-tolerance attempt for --worker; defaults to the newest. See --attempts.",
)
@click.option(
    "--attempts",
    is_flag=True,
    help="List every attempt (classified) instead of printing logs.",
)
@click.option("--instance", "instance_name", help="Exact platform instance/pod name.")
@json_option
@pass_context
def logs(
    ctx: Context,
    job_id: Optional[str],
    tail: int | None,
    head: int | None,
    worker: int,
    attempt: int | None,
    attempts: bool,
    instance_name: str | None,
    json_output: bool = False,
) -> None:
    """View logs for a training job.

    Reads the platform log API, which returns the container output of one worker's attempt. The
    newest attempt is chosen unless --attempt or --instance names one; --attempts lists every
    attempt, classified, instead of printing logs.

    \b
    Examples:
        inspire job logs <job-id>
        inspire job logs <job-id> --tail 100
        inspire job logs <job-id> --head 50
        inspire job logs <job-id> --worker 1
        inspire job logs <job-id> --attempts
    """

    json_output = resolve_json_output(ctx, json_output)

    if not job_id:
        _handle_error(
            ctx,
            "InvalidUsage",
            "job logs requires a JOB_ID",
            EXIT_VALIDATION_ERROR,
        )
        return

    job_id = resolve_job_id(ctx, job_id)

    if attempts:
        _describe_job_attempts(ctx, job_id=job_id)
        return

    _run_platform_job_logs(
        ctx,
        job_id=job_id,
        tail=tail,
        head=head,
        worker=worker,
        instance_name=instance_name,
        attempt=attempt,
    )


__all__ = ["logs"]
