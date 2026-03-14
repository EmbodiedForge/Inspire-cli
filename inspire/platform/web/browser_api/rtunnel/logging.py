"""Structured trace helpers for rtunnel/browser automation flows."""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

_TRACE_LOG = __import__("logging").getLogger("inspire.platform.web.browser_api.rtunnel.trace")

_CURRENT_TRACE: ContextVar["RtunnelTrace | None"] = ContextVar(
    "inspire_rtunnel_trace",
    default=None,
)
_LAST_FAILURE_SUMMARY: str | None = None


def _redact_text(text: str) -> str:
    import re

    value = str(text or "")
    if not value:
        return value

    query_token_re = re.compile(r"(?i)([?&](?:token|access_token|refresh_token)=)([^&\s]+)")
    path_token_re = re.compile(r"(/(?:jupyter|vscode)/[^/]+/)([^/]+)(/proxy/)")
    field_re = re.compile(
        r"""(?ix)
        (
            [\"']?
            (?:
                password|passwd|token|access[_-]?token|refresh[_-]?token|
                secret|api[_-]?key|authorization|cookie|set-cookie
            )
            [\"']?
            \s*[:=]\s*
        )
        (
            \"[^\"]*\" | '[^']*' | [^\s,}\]]+
        )
        """
    )
    value = field_re.sub(r"\1<redacted>", value)
    value = query_token_re.sub(r"\1<redacted>", value)
    value = path_token_re.sub(r"\1<redacted>\3", value)
    return value


def _sanitize_value(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _redact_text(str(value))


def _debug_enabled() -> bool:
    value = os.environ.get("INSPIRE_RTUNNEL_DEBUG", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class RtunnelTrace:
    run_id: str
    notebook_id: str
    account: str | None
    port: int
    ssh_port: int
    headless: bool
    events: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, str] = field(default_factory=dict)


def create_trace(
    *,
    notebook_id: str,
    account: str | None,
    port: int,
    ssh_port: int,
    headless: bool,
) -> RtunnelTrace:
    return RtunnelTrace(
        run_id=uuid.uuid4().hex[:8],
        notebook_id=notebook_id,
        account=account,
        port=port,
        ssh_port=ssh_port,
        headless=headless,
    )


@contextmanager
def bind_trace(trace: RtunnelTrace) -> Iterator[RtunnelTrace]:
    token = _CURRENT_TRACE.set(trace)
    try:
        yield trace
    finally:
        _CURRENT_TRACE.reset(token)


def get_current_trace() -> RtunnelTrace | None:
    return _CURRENT_TRACE.get()


def clear_last_failure_summary() -> None:
    global _LAST_FAILURE_SUMMARY
    _LAST_FAILURE_SUMMARY = None


def set_last_failure_summary(summary: str | None) -> None:
    global _LAST_FAILURE_SUMMARY
    _LAST_FAILURE_SUMMARY = summary


def get_last_failure_summary() -> str | None:
    return _LAST_FAILURE_SUMMARY


def update_trace_summary(**values: object) -> None:
    trace = get_current_trace()
    if trace is None:
        return
    for key, value in values.items():
        if value is None:
            continue
        trace.summary[str(key)] = _sanitize_value(value)


def trace_event(event: str, **fields: object) -> None:
    trace = get_current_trace()
    base_fields: dict[str, str] = {}
    if trace is not None:
        base_fields = {
            "run_id": trace.run_id,
            "notebook_id": trace.notebook_id,
            "account": _sanitize_value(trace.account),
            "port": str(trace.port),
            "ssh_port": str(trace.ssh_port),
        }

    safe_fields = {
        str(key): _sanitize_value(value) for key, value in fields.items() if value is not None
    }
    if trace is not None:
        trace.events.append({"event": event, **safe_fields})

    combined = {**base_fields, **safe_fields}
    kv = " ".join(f"{key}={value}" for key, value in combined.items())
    _TRACE_LOG.debug("%s %s", event, kv)

    if _debug_enabled():
        if kv:
            sys.stderr.write(f"  [rtunnel-debug] {event} | {kv}\n")
        else:
            sys.stderr.write(f"  [rtunnel-debug] {event}\n")
        sys.stderr.flush()


def format_trace_summary(trace: RtunnelTrace | None = None) -> str:
    trace = trace or get_current_trace()
    if trace is None:
        return ""

    lines = [
        "RTunnel trace summary:",
        f"  run_id={trace.run_id}",
        f"  notebook_id={trace.notebook_id}",
        f"  headless={_sanitize_value(trace.headless)}",
    ]
    if trace.account:
        lines.append(f"  account={_sanitize_value(trace.account)}")

    for key in (
        "lab_resolution",
        "terminal_transport",
        "setup_confirmed",
        "setup_errors",
        "bootstrap_mode",
        "bootstrap_strategy",
        "rtunnel_source",
        "upload_policy",
        "proxy_probe_result",
        "probe_diagnostics",
        "diagnosis_observed",
        "diagnosis_excerpt",
        "proxy_url",
        "screenshot_path",
        "last_error",
    ):
        value = trace.summary.get(key)
        if value:
            lines.append(f"  {key}={value}")

    recent = trace.events[-5:]
    if recent:
        lines.append("  recent_events=")
        for item in recent:
            event = item.get("event", "")
            details = " ".join(f"{key}={value}" for key, value in item.items() if key != "event")
            if details:
                lines.append(f"    {event} {details}")
            else:
                lines.append(f"    {event}")

    return "\n".join(lines)


def attach_failure_summary(message: str, trace: RtunnelTrace | None = None) -> str:
    summary = format_trace_summary(trace)
    if not summary:
        return message
    return f"{message}\n\n{summary}"
