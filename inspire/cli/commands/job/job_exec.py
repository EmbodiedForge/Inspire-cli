"""``inspire job exec`` v2: every answer comes from the pod, never from the transport.

Why this replaces the v1 verb instead of extending it. The v1 channel was a browser PTY, so the CLI
had to infer "what happened" from a terminal stream that drops without warning: exit codes came back
wrapped in a shell frame, stdout and stderr came back merged, and a lost channel left the caller
holding a command that may or may not have run. execd v2 moves the answer onto the pod - an exec is
a record with an id, a state, separate stdout/stderr logs and byte offsets, and every question about
it is a fresh read. That makes this verb's job declarative: dispatch, then observe. Four properties
follow from it, and each is pinned by tests:

* **Two streams, promised.** ``stream_model: "separate"`` means an empty ``stderr`` says nothing was
  written to stderr - not that the transport merged it away, which is what v1's ``stderr: null`` had
  to mean. stdout is echoed to stdout and stderr to stderr in every mode that shows output.
* **A local wait is not a kill switch, and it is not a deadline either.** Watching runs until the exec
  reaches a terminal state unless the caller names ``--timeout``, because a command that takes longer
  than a number nobody chose is not a failed command. When a budget *is* named, the exec keeps running,
  the process exits 124, and the report carries the ``exec_id`` plus the offsets reached, so the next
  call resumes with ``--attach --after`` instead of re-sending a command that may have written
  something. Ctrl-C is the same event with a different cause (exit 130): it only ever detaches the
  watcher, because interrupting the run is ``--cancel``'s job.
* **Nothing is ignored.** v1 collected options that did nothing on some paths - the defect the
  surface ablation (#57) keeps finding. Here the supplied options are checked against the selected
  mode before anything is sent, and an option that cannot act on that mode is a validation error
  naming it.
* **Report verbs carry no runtime environment.** ``--status``, ``--attach``, ``--list``,
  ``--cancel``, ``--cleanup`` and ``--daemon-status`` run no command, so they load no ``[remote_env]``,
  no venv and no ``cd``: the resolved prefix, which can hold secrets expanded from the local
  environment, stays out of calls that only read, and those verbs keep working from a
  credential-only config. When a command *is* run, the prefix wraps only what is sent - the report
  echoes the caller's own text under ``command``.

``--workers`` is the same channel over N pods: one execd client per pod, so each pod answers with
its own record, its own two streams and its own exit code, and the sweep's payload keeps the batch's
exit-code conventions (124 for a pod this process stopped watching, else the largest non-zero code).
"""

from __future__ import annotations

import codecs
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence

import click
from click.core import ParameterSource

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_TIMEOUT,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.config import ConfigError
from inspire.platform.execd.bootstrap import TRANSPORT_CODES
from inspire.platform.execd.protocol import (
    PROTOCOL_VERSION,
    TERMINAL_STATES,
    ExecdError,
    new_exec_id,
    validate_exec_id,
    validate_session,
)
from inspire.platform.web import browser_api as _browser_api
from inspire.platform.web.browser_api.jobs import (
    JobInstanceNotAttachableError,
    JobReadinessTimeoutError,
    describe_instance,
)

from .job_commands import (
    _job_exec_runtime_prefix,
    _normalize_exec_command,
    parse_worker_spec,
)

#: execd keeps stdout and stderr apart, so a caller may read an empty stderr as "nothing was
#: written there" - the distinction the PTY channel could not make and answered with `null`.
#: Every mode this verb has now answers under it, the pod fan-out included.
STREAM_MODEL = "separate"

#: ``--timeout`` has no numeric default, and the reason is the caller who never names it. A bare
#: ``inspire job exec <job> -- make`` asks for the command's answer; a default of 60 s answered it with
#: exit 124 for a command that was merely still running, so the only caller who never heard of
#: ``--detach`` was the one this misled. Not naming the option means "watch to the terminal state and
#: give me its code"; naming it means "stop watching at N seconds and hand me the cursor instead".
#: Neither is one call held open past the channel's patience - the watch is a page at a time, each
#: long-poll capped by the client's own ``MAX_FOLLOW_WAIT``.
#: Bytes read per stream by ``--attach``/``--workers``, and the most a ``--json`` payload copies per
#: stream. Offsets keep counting past the cap, so a cap never hides progress.
DEFAULT_MAX_BYTES = 65536
#: v1 capped one exec command at 2048 bytes because the PTY line ran into ``MAX_CANON``. A v2
#: command travels as a framed payload to the agent, so the only reason left to bound it is local
#: memory - not a transport that loses the tail without saying so.
COMMAND_MAX_BYTES = 1024 * 1024

# --- the --workers sweep -------------------------------------------------------------------
#: Pods probed at once. Session establishment is the step measured as flaky on this transport, so a
#: sweep spreads its sessions rather than opening one per pod in the same instant.
DEFAULT_FANOUT_PARALLEL = 4
#: Bytes of each stream kept per pod when the caller did not name ``--max-bytes``. A sweep multiplies
#: everything it holds by the number of pods, so its ceiling is its own, below a single read's.
FANOUT_MAX_BYTES = 16384
#: Seconds of offset between two pods' session opens inside one concurrency slot, because the gateway
#: rate-limits connection bursts: a sweep that starts 32 sessions at once loses some to its own burst.
FANOUT_STAGGER_SECONDS = 0.25
#: The per-pod verdicts a sweep reports. ``no_answer`` is the one a caller cannot resolve from here -
#: this process stopped watching, so the pod's exec may still be running - which is why it outranks a
#: known failure in the exit code, exactly as it does in a batch.
FANOUT_STATUSES = ("ok", "command_failed", "no_answer", "unreachable", "not_schedulable", "absent")
#: Ctrl-C's own convention (128 + SIGINT), as `bridge ssh` already uses it. Distinct from 124 so a
#: caller can tell "the watcher was interrupted" from "the watcher's budget ran out".
EXIT_INTERRUPTED = 130
#: "We stopped watching in time" - the number v1 used for a command that answered nothing, with the
#: same meaning: the local budget ended, the remote outcome is unknown, the id is the follow-up.
EXIT_LOCAL_TIMEOUT = 124

_AFTER_RE = re.compile(r"^(\d+):(\d+)$")


def _sleep(seconds: float) -> None:
    """The one place this module blocks the clock, so a test can step over it."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Reading whatever the client hands back, without pinning its exact spelling
# ---------------------------------------------------------------------------


def _as_mapping(value: Any) -> Dict[str, Any] | None:
    """Best-effort view of a client answer as a mapping.

    The client belongs to another module and may answer with a dict, a dataclass or a record type;
    reading them alike here keeps a cosmetic change on that side from turning a successful exec into
    a report that says nothing.
    """
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes, int, float, bool)) or value is None:
        return None
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return dict(items())
        except Exception:  # noqa: BLE001 - not a mapping after all; fall through to attribute view
            pass
    try:
        return dict(vars(value))
    except TypeError:
        return None


def _field(record: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if record.get(name) is not None:
            return record[name]
    return default


def _rows_of(values: Sequence[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in values:
        row = _as_mapping(item)
        if row:
            rows.append(row)
    return rows


def _records_of(response: Any) -> List[Dict[str, Any]]:
    """Normalize a dispatch or list answer into one record per exec.

    One command answers with one record; a batch may answer with a record per command, either bare
    or under a wrapper key. Those mean the same thing to a caller, so they come back the same way
    here instead of as two payload layouts.
    """
    mapping = _as_mapping(response)
    if mapping is not None:
        for key in ("execs", "records", "results", "items"):
            value = mapping.get(key)
            if isinstance(value, (list, tuple)):
                return _rows_of(value) or [mapping]
        return [mapping]
    if isinstance(response, (list, tuple)):
        return _rows_of(response)
    return []


def _state_of(record: Mapping[str, Any]) -> str:
    return str(_field(record, "state", "status", default="") or "").strip().lower()


def _exit_code_of(record: Mapping[str, Any]) -> int | None:
    value = _field(record, "exit_code", "returncode", "rc")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _offsets_of(record: Mapping[str, Any]) -> Dict[str, int]:
    """Where the pod's two logs currently end, as the record states it.

    Absent fields read as 0 rather than missing: a caller resuming with ``--after`` needs two
    numbers, and "we could not tell" must not quietly become "start over from 0".
    """
    raw = record.get("offsets")
    source: Mapping[str, Any] = raw if isinstance(raw, Mapping) else record
    offsets: Dict[str, int] = {}
    for stream, names in (
        ("stdout", ("stdout", "stdout_offset", "stdout_end", "next_stdout_offset")),
        ("stderr", ("stderr", "stderr_offset", "stderr_end", "next_stderr_offset")),
    ):
        declared = _window_next(record, stream)
        if declared is not None:
            offsets[stream] = declared
            continue
        value = _field(source, *names)
        if value is not None and not isinstance(value, Mapping):
            offsets[stream] = _as_int(value, 0)
    return {"stdout": 0, "stderr": 0, **offsets}


def _is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


#: The keys that make a stream value an output *window* rather than a plain body. ``bytes`` is in the
#: list as a marker only - on this client it counts the page, so it is never read as a payload.
_WINDOW_KEYS = ("offset", "next_offset", "bytes", "eof", "text", "data")


#: What a chunk's payload may be raw. The channel carries bytes; text is accepted as well, because a
#: different client could hand over a decoded string and losing output over a type would be the
#: worse failure.
_RAW_TYPES = (bytes, bytearray, memoryview)


def _decode_payload(value: Any) -> str:
    """Render one payload as text, decoding bytes the way a terminal would."""
    if isinstance(value, str):
        return value
    if isinstance(value, _RAW_TYPES):
        return bytes(value).decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def _window_of(record: Mapping[str, Any], stream: str) -> Optional[Dict[str, Any]]:
    """One stream's value as an output window, or None when it arrived as a plain body.

    A page answer carries each stream with its position in the log - where this page started, where
    the next read should ask from, whether the stream is finished - while an older or simpler answer
    carries only the text. Both mean the same thing to a caller, and only one of them can say where
    to resume, so the difference is read here rather than guessed at each use.
    """
    mapping = _as_mapping(record.get(stream))
    if mapping is not None and any(key in mapping for key in _WINDOW_KEYS):
        return mapping
    return None


def _body_of(record: Mapping[str, Any], stream: str) -> str:
    """One stream's text, from a window or from a bare body."""
    window = _window_of(record, stream)
    if window is None:
        return str(_field(record, stream, default="") or "")
    return _decode_payload(_field(window, "text", "data", "chunk", "payload", default=""))


def _window_next(record: Mapping[str, Any], stream: str) -> Optional[int]:
    """Where the pod says the next read of this stream starts, when the answer says so.

    This is the authoritative resume point: it counts the pod's bytes, not this process's reading of
    them, so it wins over a count derived from decoded text (which can differ once a lossy decode or
    a split multi-byte character is involved).
    """
    window = _window_of(record, stream)
    if window is None:
        return None
    value = window.get("next_offset")
    return None if value is None else _as_int(value, 0)


def _window_bytes(record: Mapping[str, Any], stream: str) -> Optional[int]:
    """How many bytes this page carried for a stream, when the answer counts them for us."""
    window = _window_of(record, stream)
    if window is None:
        return None
    value = window.get("bytes")
    return None if value is None else _as_int(value, 0)


def _window_eof(record: Mapping[str, Any], stream: str) -> Optional[bool]:
    window = _window_of(record, stream)
    if window is not None and "eof" in window:
        return bool(window["eof"])
    key = f"{stream}_eof"
    if key in record:
        return bool(record[key])
    end = record.get(f"{stream}_end")
    next_offset = _window_next(record, stream)
    if end is not None and next_offset is not None:
        return next_offset >= _as_int(end, 0)
    return None


# ---------------------------------------------------------------------------
# Streaming: the byte offsets this process actually consumed
# ---------------------------------------------------------------------------


class _Streams:
    """Per-stream capture with live echo, so ``--json`` still emits exactly one document.

    Offsets are byte counts of what this process consumed, which is exactly what ``--after`` takes
    on the next call. In JSON mode nothing is echoed while the exec runs - the contract puts one
    document on stdout - so chunks are buffered and travel inside it. The buffer is capped, the
    counter is not, so a caller can always tell from ``offsets`` that there was more.
    """

    def __init__(self, *, live: bool, cap: int = DEFAULT_MAX_BYTES) -> None:
        self.live = live
        self.cap = max(cap, 1)
        self.bodies: Dict[str, List[str]] = {"stdout": [], "stderr": []}
        self.offsets: Dict[str, int] = {"stdout": 0, "stderr": 0}
        self.truncated = False
        self._captured_bytes: Dict[str, int] = {"stdout": 0, "stderr": 0}
        #: True once text has reached the terminal, so the caller does not print the same bytes twice.
        self.echoed = False
        #: One incremental decoder per stream, because a chunk boundary may land inside a character and
        #: a per-chunk decode would turn the halves into two replacement marks.
        self._decoders: Dict[str, Any] = {
            stream: codecs.getincrementaldecoder("utf-8")(errors="replace")
            for stream in ("stdout", "stderr")
        }

    def _decode(self, stream: str, payload: Any) -> tuple[str, int]:
        """Turn one chunk into text, plus how far the pod's log really advanced.

        Bytes are counted as received: the log position moves by every byte the pod wrote, including
        one a decoder is still holding because a character was split in half. Counting the decoded
        text instead would report a position short of where the bytes were read to, and the next
        ``--after`` would re-show bytes the caller has already seen.
        """
        if isinstance(payload, Mapping):
            payload = _field(payload, "text", "data", "chunk", "payload", default="")
        if isinstance(payload, _RAW_TYPES):
            raw = bytes(payload)
            return self._decoders[stream].decode(raw), len(raw)
        text = payload if isinstance(payload, str) else _decode_payload(payload)
        return text, len(text.encode("utf-8"))

    def on_output(self, *args: Any, **kwargs: Any) -> None:
        """Accept whatever chunk shape the client hands the follow callback.

        The intended shape is ``(stream, bytes)`` - the client streams the log's raw bytes - and a
        ``(stream, str)`` pair, a mapping or a bare string is accepted too, because the chunk type
        belongs to the client and losing output over a signature mismatch would be the worse failure.
        """
        stream, payload = _split_chunk(args, kwargs)
        if not payload or stream not in self.bodies:
            return
        text, consumed = self._decode(stream, payload)
        self.offsets[stream] += consumed
        if not text:
            return
        if self.live:
            click.echo(text, nl=False, err=stream == "stderr")
            self.echoed = True
            return
        room = self.cap - self._captured_bytes[stream]
        if room <= 0:
            self.truncated = True
            return
        encoded = text.encode("utf-8")
        if len(encoded) <= room:
            kept = text
        else:
            # Decoding the byte prefix drops only an incomplete trailing character. `offsets` still
            # counts every consumed byte, so the uncaptured suffix can be resumed with --attach.
            kept = encoded[:room].decode("utf-8", errors="ignore")
            self.truncated = True
        if kept:
            self.bodies[stream].append(kept)
            self._captured_bytes[stream] += len(kept.encode("utf-8"))

    def adopt(self, body: str, stream: str) -> None:
        """Take a body that arrived as a whole (an attach window) into this stream's account."""
        if not body:
            return
        size = len(body.encode("utf-8"))
        self.bodies[stream].append(body)
        self._captured_bytes[stream] += size
        self.offsets[stream] += size

    def text(self, stream: str) -> str:
        return "".join(self.bodies.get(stream) or [])

    def summary(self) -> Dict[str, Any]:
        return {
            "stdout": self.text("stdout"),
            "stderr": self.text("stderr"),
            # Streams that carried nothing contribute no offset, so the record's own position
            # survives: a resumed attach must not report 0 for a log this call read no bytes of.
            "offsets": {key: value for key, value in self.offsets.items() if value},
            "truncated": self.truncated,
        }


def _split_chunk(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> tuple[str, Any]:
    """Reduce one follow callback's arguments to ``(stream, payload)``.

    The payload is passed through as it arrived - bytes stay bytes - so the caller can count the
    pod's bytes rather than the characters they decoded to.
    """
    # The intended shape first: `on_output("stderr", b"…")`. Checked before the bare-string case,
    # because iterating a two-part call would otherwise read the stream name as the payload.
    if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], (str, *_RAW_TYPES)):
        return _normalize_stream(args[0]), args[1]
    values: List[Any] = list(args)
    if kwargs:
        values.append(dict(kwargs))
    for value in values:
        mapping = _as_mapping(value)
        if mapping is not None:
            named = [key for key in ("stdout", "stderr") if key in mapping]
            if len(named) == 1:
                return named[0], mapping[named[0]]
            stream = _normalize_stream(
                str(mapping.get("stream") or mapping.get("name") or mapping.get("fd") or "stdout")
            )
            for key in ("text", "data", "chunk", "line"):
                if mapping.get(key) is not None:
                    return stream, mapping[key]
            return stream, ""
        if isinstance(value, tuple) and len(value) == 2:
            return _normalize_stream(str(value[0])), value[1]
        if isinstance(value, _RAW_TYPES):
            # A bare payload is stdout, for the same reason a bare string is: stderr arrives named.
            return "stdout", bytes(value)
        if isinstance(value, str):
            # A bare string is stdout: stderr always arrives named, and guessing otherwise would put
            # error text where a caller's pipe expects results.
            return "stdout", value
    return "stdout", ""


def _normalize_stream(stream: str) -> str:
    text = stream.strip().lower()
    return "stderr" if text in ("2", "err", "stderr") else "stdout"


# ---------------------------------------------------------------------------
# Client access, and the error vocabulary shared by every mode
# ---------------------------------------------------------------------------


def _client_class() -> Any:
    """Import seam for the execd client: the client owns the transport, this module does not."""
    from inspire.platform.execd.client import JobExecdClient

    return JobExecdClient


def _open_client(
    job_id: str,
    *,
    worker: int,
    attempt: int | None,
    instance_name: str | None,
    wait_timeout: int,
) -> Any:
    """One client per invocation, aimed at exactly one pod.

    ``web_session`` is left to the client on purpose: the CLI's ``--account`` override is process
    state the platform layer already honours, and building a session here would mean authenticating
    for verbs that may never need it.
    """
    return _client_class()(
        job_id,
        worker=worker,
        instance_name=instance_name,
        attempt=attempt,
        wait_timeout=wait_timeout,
        share_session=True,
    )


def _report_readiness_timeout(
    ctx: Context, exc: JobReadinessTimeoutError, *, exec_id: str | None = None
) -> None:
    """Report a pod this call could not get to, and stop there.

    Nothing reached the pod: a client-side wait expired, which is not an exec outcome. Exit 14 like
    `job wait`, never 124, which would claim a run this call never started. A follow that lost its
    channel and then found no job to reopen one on lands here too - the exec it was reading may well
    have finished, but this process cannot say so, and 14 is the difference between "the pod was not
    there" and "the answer was lost on the way".
    """
    follow_up = f" and retry --attach {exec_id}: the exec itself is untouched." if exec_id else "."
    _handle_error(
        ctx,
        "Timeout",
        str(exc),
        EXIT_TIMEOUT,
        hint=(
            "nothing was sent to the pod, so this call changed nothing there. Watch the job with "
            f"`inspire --json job wait <job> --until running`, or raise --wait-timeout{follow_up}"
        ),
    )


def _channel_notice(*, label: Optional[str] = None) -> Callable[[Mapping[str, Any]], None]:
    """The callback that says, once, that the channel died and the reading did not.

    One line, on stderr, in both output modes: the contract puts a single document on stdout, so the
    fact belongs on the other stream. It is deliberately not reassuring and not dimmed - the caller is
    watching a command that outlived the connection to it, and the seconds, the id and the two offsets
    are everything that claim needs. Silent resuming would make the gap look like a hang, and a
    cheerful one would make a real failure look like this one.
    """

    def notice(info: Mapping[str, Any]) -> None:
        offsets = _as_mapping(info.get("offsets")) or {}
        stdout_at = int(offsets.get("stdout", 0) or 0)
        stderr_at = int(offsets.get("stderr", 0) or 0)
        text = (
            f"channel lost after {float(info.get('elapsed') or 0.0):.0f}s: "
            f"exec {info.get('exec_id')} reconnected, resuming from "
            f"stdout {stdout_at} / stderr {stderr_at}"
        )
        click.echo(f"[{label}: {text}]" if label else text, err=True)

    return notice


def _observe(ctx: Context, action: Callable[[], Any], *, exec_id: str | None = None) -> Any:
    """Run one client call, translating its failures into the CLI's error vocabulary.

    ``_handle_error`` exits, so a caller holding a return value knows the call happened.
    KeyboardInterrupt and SystemExit are deliberately not caught: only the follow loop detaches
    locally, and swallowing Ctrl-C on a one-shot read would trap the caller inside a command with
    nothing left to stream.
    """
    try:
        return action()
    except JobReadinessTimeoutError as exc:
        _report_readiness_timeout(ctx, exc, exec_id=exec_id)
    except JobInstanceNotAttachableError as exc:
        # The pod's own record - terminal status *and* `finished_at` - is what names this, and the
        # reader's next move is the opposite of the one a lost channel suggests: there is no exec to
        # read back, because nothing was sent, and no retry makes a finished round host a command.
        # Left in the generic branch it would have read as unclassified transport churn and sent the
        # caller to --status on a record that was never created.
        _handle_error(
            ctx,
            "ExecError",
            str(exc),
            EXIT_API_ERROR,
            hint=(
                "the pod's own record says this round is over, so this is not the exec channel "
                "being unstable and there is nothing to read back with --status. "
                "`inspire job instances <job>` lists every attempt with its node and status, "
                "`inspire job logs <job>` reads the one that ended, and --instance/--attempt aims "
                "at a round that is still live."
            ),
        )
    except ExecdError as exc:
        _report_execd_error(ctx, exc, exec_id=exec_id)
    except Exception as exc:  # noqa: BLE001 - classified below, never silently swallowed
        text = str(exc)
        hint = (
            "the exec channel failed for a reason the agent did not classify; nothing was retried. "
            "Read the exec's own state with --status before doing anything that could duplicate a "
            "side effect."
        )
        if any(
            marker in text.lower()
            for marker in ("connection reset", "broken pipe", "timed out", "connection lost")
        ):
            hint = (
                "this looks like transport churn. Retry only a command with no side effects: a run "
                "we lost contact with may still have completed. Ask with --status first when in doubt."
            )
        _handle_error(ctx, "ExecError", text, EXIT_API_ERROR, hint=hint)
    raise AssertionError("unreachable: every handler exits")  # pragma: no cover


def _report_execd_error(
    ctx: Context, exc: ExecdError, *, exec_id: str | None = None, job_id: str | None = None
) -> None:
    if exc.code in TRANSPORT_CODES:
        attempts = exc.data.get("attempts")
        asked = f"{attempts} attempts" if isinstance(attempts, int) else "several attempts"
        reads = f"--status {exec_id}" if exec_id else "--status <exec-id>"
        resumes = f"--attach {exec_id}" if exec_id else "--attach <exec-id>"
        reconnects = exc.data.get("reconnects")
        positions = {
            stream: exc.data.get(f"{stream}_offset")
            for stream in ("stdout", "stderr")
            if isinstance(exc.data.get(f"{stream}_offset"), int)
        }
        if len(positions) == 2:
            resumes = f"{resumes} --after {positions['stdout']}:{positions['stderr']}"
        hint = (
            f"the exec channel gave up after the client's retries - this call made {asked}, each on "
            f"a session it reopened. The request may already have reached execd, so the pod's own "
            f"record is the truth and it is still there: {reads} re-reads it, and {resumes} continues "
            f"reading its output from the offset this call stopped at; neither starts a run. Nothing "
            f"re-asks again on its own from here - repeating this call is the caller's decision, and a "
            f"report is safe to repeat where a run may not be."
        )
        if isinstance(reconnects, int):
            # A reader that kept going on its own says so, because "the channel died" and "the channel
            # stayed dead while I tried to bring it back" are different reports.
            hint += (
                f" This reader then reopened the channel {reconnects} times in a row and it stayed "
                f"down. What failed is the reading, not the exec: the bytes it had already delivered "
                f"were delivered once, and the record is still on the pod."
            )
            if len(positions) == 2 and job_id:
                hint += (
                    f" Continue this read by hand with "
                    f"`{_resume_hint(job_id, exec_id, positions)}`."
                )
    elif exc.code == "remote-command-failed":
        # The same code covers a command the pod ran and a chunk this client sent three times before it
        # gave up (bootstrap `_push_chunks`), so this branch may not promise that nothing was retried, nor
        # that the pod is as it was.
        hint = (
            "this is an answer from the pod side, not a lost channel: either a command there "
            "answered non-zero, or one of this client's own transfer attempts gave up after repeating "
            "itself. The message carries the exit code and a redacted tail, `data` repeats both, and "
            "whatever an attempt wrote on the pod is still written there."
        )
    else:
        hint = (
            "this is execd's own answer, not a transport hiccup: an answer this specific is never "
            "re-asked, so this call ran once. The fields under `data` are what it said."
        )
    _handle_error(
        ctx,
        "ExecdError",
        str(exc),
        EXIT_API_ERROR,
        hint=hint,
        data={"code": exc.code, "exec_id": exec_id, **exc.data},
    )


def _runtime_prefix(ctx: Context) -> Optional[List[str]]:
    """The runtime environment a command runs behind, or None after a refusal has been reported.

    The shared resolver reports an unloadable configuration as ``ConfigError``, while a malformed
    file can also surface straight from the TOML reader as a plain ``ValueError``. Both mean the
    same thing here - the environment this command would run in is unknown - so both are reported
    as a configuration error instead of reaching the caller as a traceback, and neither is allowed
    to fall through to a dispatch: running the command in a silently different environment is the
    failure this prefix exists to prevent.
    """
    try:
        return _job_exec_runtime_prefix(ctx)
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except ValueError as exc:
        _handle_error(
            ctx,
            "ConfigError",
            f"the runtime configuration could not be read: {exc}",
            EXIT_CONFIG_ERROR,
            hint="`inspire config check` reports which file and line the reader rejected.",
        )
    return None


def _compose_runtime_command(text: str, prefix: Sequence[str]) -> str:
    """One command behind the runtime prefix, as the pod's shell is meant to receive it.

    The prefix parts are idempotent - exports, an activate, a ``cd`` - so repeating them per command
    of a batch costs characters and buys the guarantee that any single command would run in the same
    environment. Only this composed line is ever sent: the report, the echo and the JSON keep the
    caller's own text, because a ``[remote_env]`` value can hold a secret expanded locally.
    """
    body = str(text).rstrip("\r\n")
    return " && ".join([*prefix, body]) if prefix else body


# ---------------------------------------------------------------------------
# Mode resolution: refuse every combination this verb cannot honour
# ---------------------------------------------------------------------------

#: Modes that observe the pod instead of running a command, keyed by the option that selects them.
_REPORT_MODES: Dict[str, str] = {
    "--attach": "attach",
    "--status": "status",
    "--cancel": "cancel",
    "--cleanup": "cleanup",
    "--list": "list",
    "--daemon-status": "daemon",
}


#: Signature name -> the label a caller typed. Click knows which options were actually named, and
#: that is the only honest answer to "was this supplied": `--workers all --worker 0` names a target
#: this mode will not use, and a check that only fires on a non-default value is a hole in the
#: "nothing is ignored" promise rather than the enforcement of it.
_OPTION_SOURCES: tuple[tuple[str, str], ...] = (
    ("command", "-- CMD"),
    ("commands", "-c/--command"),
    ("stdin", "--stdin"),
    ("detach", "--detach"),
    ("follow", "--follow"),
    ("after", "--after"),
    ("max_bytes", "--max-bytes"),
    ("session", "--session"),
    ("worker", "--worker"),
    ("attempt", "--attempt"),
    ("instance_name", "--instance"),
    ("workers_spec", "--workers"),
    ("parallel", "--parallel"),
    ("summary_only", "--summary-only"),
    ("timeout", "--timeout"),
    ("wait_timeout", "--wait-timeout"),
    ("attach_id", "--attach"),
    ("status_id", "--status"),
    ("cancel_id", "--cancel"),
    ("cleanup_id", "--cleanup"),
    ("list_records", "--list"),
    ("daemon_status", "--daemon-status"),
)


def _supplied_labels() -> FrozenSet[str]:
    """The labels of every option named on this command line, whatever value it was given."""
    try:
        click_ctx = click.get_current_context()
    except RuntimeError:  # pragma: no cover - the callback was driven outside a click run
        return frozenset()
    return frozenset(
        label
        for name, label in _OPTION_SOURCES
        if click_ctx.get_parameter_source(name) is ParameterSource.COMMANDLINE
    )


class _Invocation:
    """What the caller supplied, and the mode those options add up to.

    The mode is decided before anything is sent so that a combination with no meaning is rejected
    as itself (`--status and --cancel are mutually exclusive`) rather than resolved by whichever
    branch came first in a dispatch chain.
    """

    def __init__(self, given: Iterable[str] = (), **values: Any) -> None:
        self.given = frozenset(given)
        self.command: tuple[str, ...] = values["command"]
        self.commands: tuple[str, ...] = values["commands"]
        self.stdin: bool = values["stdin"]
        self.detach: bool = values["detach"]
        self.attach_id: Optional[str] = values["attach_id"]
        self.status_id: Optional[str] = values["status_id"]
        self.cancel_id: Optional[str] = values["cancel_id"]
        self.cleanup_id: Optional[str] = values["cleanup_id"]
        self.list_records: bool = values["list_records"]
        self.daemon_status: bool = values["daemon_status"]
        self.follow: bool = values["follow"]
        self.after: str = values["after"]
        self.max_bytes: int = values["max_bytes"]
        self.session: Optional[str] = values["session"]
        self.worker: int = values["worker"]
        self.attempt: Optional[int] = values["attempt"]
        self.instance_name: Optional[str] = values["instance_name"]
        self.workers_spec: Optional[str] = values["workers_spec"]
        self.parallel: int = values["parallel"]
        self.summary_only: bool = values["summary_only"]
        self.timeout: Optional[float] = values["timeout"]
        self.wait_timeout: int = values["wait_timeout"]
        self.session_bootstrap: Optional[str] = None

        self.mode = ""
        self.exec_id: Optional[str] = None
        self.fanout = self.workers_spec is not None
        self.requested_workers: Optional[List[int]] = None
        self.after_offsets: Dict[str, int] = {"stdout": 0, "stderr": 0}

    @property
    def ids(self) -> Dict[str, Optional[str]]:
        """The id-taking options by their label, in a fixed order."""
        return {
            "--attach": self.attach_id,
            "--status": self.status_id,
            "--cancel": self.cancel_id,
            "--cleanup": self.cleanup_id,
        }

    @property
    def flags(self) -> Dict[str, bool]:
        return {"--list": self.list_records, "--daemon-status": self.daemon_status}

    def supplied(self) -> Dict[str, bool]:
        """Label -> did this call give the option something to do?

        An option counts when the caller named it (whatever its value) or when its value differs
        from the default. Naming is what catches `--workers all --worker 0`: the value is the
        default, and the intent - naming a pod a fan-out will not use - is still on the line. A
        call built without a click context (a test, or a direct import) still sees every
        non-default value, which is why both rules are here rather than only the first.
        """
        named = self.given
        return {
            "-- CMD": bool(self.command) or "-- CMD" in named,
            "--stdin": self.stdin,
            "-c/--command": bool(self.commands),
            "--detach": self.detach,
            "--follow": self.follow,
            "--after": self.after != "0:0" or "--after" in named,
            "--max-bytes": self.max_bytes != DEFAULT_MAX_BYTES or "--max-bytes" in named,
            "--session": bool(self.session),
            "--worker": self.worker != 0 or "--worker" in named,
            "--attempt": self.attempt is not None,
            "--instance": bool(self.instance_name),
            "--workers": self.workers_spec is not None,
            "--parallel": self.parallel != DEFAULT_FANOUT_PARALLEL or "--parallel" in named,
            "--summary-only": self.summary_only,
            # The option has no default, so a number *is* the evidence that it was named - and naming
            # is read from click's parameter source (`_supplied_labels`), which is also what lets a
            # call built without a click context still see the budget it was handed.
            "--timeout": self.timeout is not None or "--timeout" in named,
            "--wait-timeout": self.wait_timeout != 0 or "--wait-timeout" in named,
            **{label: bool(value) for label, value in self.ids.items()},
            **self.flags,
        }

    def command_sources(self) -> List[str]:
        given = self.supplied()
        return [label for label in ("-- CMD", "--stdin", "-c/--command") if given[label]]

    def used(self, *labels: str) -> bool:
        given = self.supplied()
        return any(given.get(label) for label in labels)


def _resolve_mode(ctx: Context, call: _Invocation) -> bool:
    """Pick the mode. False means the refusal has been reported; the caller must return."""
    requested = [label for label, value in call.ids.items() if value is not None] + [
        label for label, given in call.flags.items() if given
    ]
    for label, value in call.ids.items():
        if value is not None and not str(value).strip():
            # `--status ""` would otherwise read as "no command given" and send the caller off to
            # look at their command text instead of at the empty id they just passed.
            return _refuse(ctx, f"{label} needs a non-empty exec id (from `job exec --detach`)")
    if len(requested) > 1:
        # One call, one question to the pod: two of these would have to decide whose answer to print.
        return _refuse(ctx, " and ".join(requested) + " are mutually exclusive")

    sources = call.command_sources()
    if len(sources) > 1:
        return _refuse(
            ctx,
            " and ".join(sources) + " each supply a command; give it one way",
            hint="`-- CMD` for one command, several -c flags for a batch, --stdin for a script",
        )

    if requested:
        if sources:
            # A report verb runs nothing, so a command supplied alongside it would be dropped in
            # silence - the one failure this verb refuses to produce.
            return _refuse(
                ctx,
                " and ".join(requested)
                + " reads the pod and takes no command; drop "
                + ", ".join(sources),
            )
        label = requested[0]
        # `--attach` is the report verb that can also watch, so its own shaping options are legal
        # with it; on --status/--list/--cancel/--cleanup they would be read and then dropped.
        legal_here = {"--follow", "--after", "--max-bytes"} if label == "--attach" else set()
        given = call.supplied()
        extras = [
            opt
            for opt in ("--detach", "--session", "--workers", "--follow", "--after", "--max-bytes")
            if given[opt] and opt not in legal_here
        ]
        if extras:
            return _refuse(
                ctx,
                " and ".join(requested)
                + " reports on one exec; "
                + ", ".join(extras)
                + " belongs to a mode that runs or reads a run",
            )
        call.mode = _REPORT_MODES[label]
        raw_id = call.ids.get(label)
        if raw_id is not None:
            validated = _validated_exec_id(ctx, str(raw_id))
            if validated is None:
                return False
            call.exec_id = validated
        if call.mode == "attach":
            parsed = _parse_after(ctx, call.after)
            if parsed is None:
                return False
            call.after_offsets = parsed
        return True

    if call.follow:
        return _refuse(
            ctx,
            "--follow continues a run that is already on the pod, so it needs --attach EXEC_ID",
            hint="start it with `-- CMD` (which follows by default) or --detach, then follow it "
            "with --attach",
        )
    if call.used("--after"):
        return _refuse(ctx, "--after resumes an --attach window, and no exec is being attached to")
    if call.used("--max-bytes") and not call.fanout:
        # A sweep caps each pod's capture with it, so it means something there; a single-pod run
        # answers with the exec's own offsets and reads no capped window.
        return _refuse(
            ctx,
            "--max-bytes bounds a read of one exec's output; nothing here reads one",
            hint="use it with --attach, or with --workers to cap each pod's capture",
        )
    if not sources and not call.detach:
        return _refuse(
            ctx,
            "Provide a command after `--`, several -c/--command flags, or --stdin; "
            "or ask a read-only question with --list / --daemon-status",
        )
    if call.detach and not sources:
        return _refuse(ctx, "--detach needs a command to start")

    call.mode = "detach" if call.detach else "run"
    if call.fanout:
        if call.used("--detach"):
            return _refuse(
                ctx,
                "--workers reports what each pod answered, so it watches them; --detach starts one "
                "exec and walks away from it",
                hint="drop --workers to detach one exec on one pod, or lower --timeout to stop "
                "watching a sweep early - each pod's report then carries its own exec_id to "
                "resume with",
            )
        if call.used("--worker", "--attempt", "--instance"):
            return _refuse(
                ctx,
                "--workers selects its own pods, so --worker/--attempt/--instance conflict with it",
                hint="name the pods with --workers alone",
            )
        try:
            call.requested_workers = parse_worker_spec(str(call.workers_spec))
        except ValueError as exc:
            return _refuse(ctx, str(exc))
    if call.session:
        try:
            call.session = validate_session(call.session)
        except ValueError as exc:
            return _refuse(ctx, f"--session is invalid: {exc}")
    return True


#: Which options each mode can actually act on. Anything supplied outside that set is refused,
#: because a silently ignored flag is the defect this repo's surface ablation keeps finding (#57).
def _allowed_options(call: _Invocation) -> set[str]:
    allowed = {"--worker", "--attempt", "--instance", "--wait-timeout"}
    if call.mode in ("run", "detach"):
        allowed |= {"-- CMD", "--stdin", "-c/--command", "--session"}
    if call.mode == "detach":
        allowed.add("--detach")
    if call.mode == "run" or (call.mode == "attach" and call.follow):
        allowed.add("--timeout")
    if call.mode == "attach":
        allowed |= {"--attach", "--after", "--follow", "--max-bytes"}
    if call.mode in ("status", "cancel", "cleanup", "list"):
        allowed.add(f"--{call.mode}")
    if call.mode == "daemon":
        allowed.add("--daemon-status")
    if call.fanout:
        allowed |= {"--workers", "--parallel", "--summary-only", "--max-bytes", "--timeout"}
        # A sweep names its pods with --workers alone: the sweep walks the newest attempt's pods, so
        # naming one pod, or an attempt to compare against, would describe a different sweep.
        allowed -= {"--worker", "--attempt", "--instance"}
    return allowed


def _reject_unusable_options(ctx: Context, call: _Invocation) -> bool:
    allowed = _allowed_options(call)
    unusable = sorted(
        label for label, given in call.supplied().items() if given and label not in allowed
    )
    if not unusable:
        return True
    target = (
        "a --workers fan-out"
        if call.fanout
        else ("a followed run" if call.mode == "run" else f"--{call.mode}")
    )
    return _refuse(
        ctx,
        ", ".join(unusable)
        + f" has no effect on {target} and was not sent; drop it or change the mode",
        hint="`inspire job exec --help` lists which options each mode accepts",
    )


def _validate_numbers(ctx: Context, call: _Invocation) -> bool:
    if call.timeout is not None and call.timeout <= 0:
        # 0 and a negative number are not "no limit" - that is what leaving --timeout out means. Taking
        # either as an unbounded watch would turn a typo into the longest call this verb can make.
        return _refuse(ctx, "--timeout must be positive (it is a local wait budget in seconds)")
    if call.wait_timeout < 0:
        # Checked before any dispatch: a negative budget would reach the readiness loop as a wait
        # that never happens, and the caller would see a platform error instead of their own typo.
        return _refuse(ctx, "--wait-timeout cannot be negative")
    if call.max_bytes < 1:
        return _refuse(ctx, "--max-bytes must be positive")
    if call.parallel < 1:
        return _refuse(ctx, "--parallel must be at least 1")
    if call.instance_name and call.worker != 0:
        return _refuse(ctx, "--instance and --worker are mutually exclusive")
    if call.instance_name and call.attempt is not None:
        return _refuse(ctx, "--instance already names one attempt; drop --attempt or use --worker")
    return True


def _refuse(ctx: Context, message: str, hint: str | None = None) -> bool:
    """Report a usage-level rejection as a validation error (12). Always returns False."""
    _handle_error(ctx, "ValidationError", message, EXIT_VALIDATION_ERROR, hint=hint)
    return False


def _validated_exec_id(ctx: Context, value: str) -> Optional[str]:
    try:
        return validate_exec_id(value)
    except ValueError as exc:
        _refuse(ctx, str(exc))
        return None


def _parse_after(ctx: Context, value: str) -> Optional[Dict[str, int]]:
    match = _AFTER_RE.fullmatch(str(value or "").strip())
    if not match:
        _refuse(
            ctx,
            f"--after must be 'stdout:stderr' byte offsets like '1024:0' (got {value!r})",
            hint="the numbers are the ones an earlier report printed under `offsets`",
        )
        return None
    return {"stdout": int(match.group(1)), "stderr": int(match.group(2))}


def _collect_command_texts(ctx: Context, call: _Invocation) -> Optional[List[str]]:
    """The caller's own command text, from whichever source they chose. None on refusal.

    What comes back is never the runtime-prefixed line: ``[remote_env]`` values can hold secrets
    expanded from the local environment, and those must not reach stdout, terminal scrollback, a
    debug log or an agent transcript.
    """
    texts: List[str]
    if call.stdin:
        if sys.stdin.isatty():
            _refuse(
                ctx,
                "--stdin was given but stdin is a terminal; pipe the command in (`cat x.sh | ...`)",
            )
            return None
        text = sys.stdin.read().strip()
        if not text:
            _refuse(ctx, "--stdin was given empty input")
            return None
        texts = [text]
    elif call.commands:
        texts = [str(part) for part in call.commands]
        if any(not part.strip() for part in texts):
            # An empty -c would reach the pod as a command that answers nothing; catching the typo
            # here keeps it out of the report as an unexplained non-answer.
            _refuse(ctx, "-c/--command needs a non-empty command for each occurrence")
            return None
    elif call.command:
        texts = [_normalize_exec_command(call.command)]
    else:
        _refuse(ctx, "Provide a command to execute.")
        return None

    for text in texts:
        size = len(text.encode("utf-8"))
        if size > COMMAND_MAX_BYTES:
            _refuse(
                ctx,
                f"a command of {size} bytes is over the {COMMAND_MAX_BYTES} byte limit",
                hint="split it across -c flags, or upload a script with `inspire job upload` "
                "and exec the path",
            )
            return None
    return texts


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _dump(payload: Dict[str, Any]) -> str:
    """The standard success envelope.

    ``default=str`` keeps a client detail this module did not foresee from destroying a report the
    caller has already paid a round trip for.
    """
    try:
        return json_formatter.format_json(payload)
    except TypeError:
        return json.dumps(
            {"success": True, "data": payload}, indent=2, ensure_ascii=False, default=str
        )


def _exec_view(
    exec_id: str, record: Mapping[str, Any], streams: Optional[_Streams]
) -> Dict[str, Any]:
    """The per-exec fields every mode reports, in one place, so no mode reports a different shape.

    Whatever this process streamed wins over what the record carries: the streamed bytes are the
    ones the caller has been shown, and their offsets are where ``--after`` resumes from. A stream
    this call read nothing of keeps the record's own position instead of being flattened to 0.
    """
    captured = streams.summary() if streams else {}
    offsets = {**_offsets_of(record), **(captured.get("offsets") or {})}
    code = _exit_code_of(record)
    return {
        "exec_id": exec_id,
        "state": _state_of(record) or "unknown",
        "exit_code": code,
        # `returncode` is the spelling `inspire exec`/`inspire bridge exec` publish; both carry one
        # value so a single parser works across the three exec verbs.
        "returncode": code,
        "stream_model": STREAM_MODEL,
        "offsets": offsets,
        "stdout": captured.get("stdout") or _body_of(record, "stdout"),
        "stderr": captured.get("stderr") or _body_of(record, "stderr"),
        "truncated": bool(captured.get("truncated")),
    }


def _human_exec_line(view: Mapping[str, Any]) -> str:
    code = view.get("exit_code")
    offsets = view.get("offsets") or {}
    bits = [
        f"exec {view.get('exec_id') or '?'}",
        str(view.get("state") or "unknown"),
        f"exit {code}" if code is not None else "no exit code",
        f"stdout {offsets.get('stdout', 0)}B / stderr {offsets.get('stderr', 0)}B",
    ]
    if view.get("timed_out"):
        bits.append("LOCAL WAIT EXPIRED - the exec is still running on the pod")
    if view.get("interrupted"):
        bits.append("WATCHER INTERRUPTED - the exec is still running on the pod")
    return "  ".join(bits)


def _resume_hint(job_id: str, exec_id: Any, offsets: Optional[Mapping[str, Any]] = None) -> str:
    """The exact command that picks a record up where this process left off.

    Spelled out rather than implied, because it is what a caller who lost the channel needs most, and
    because a resumed read must start at the offsets just reported - not at 0, which would show the
    same bytes twice.
    """
    positions = offsets or {}
    return (
        f"inspire job exec {job_id} --attach {exec_id} "
        f"--after {int(positions.get('stdout', 0) or 0)}:{int(positions.get('stderr', 0) or 0)}"
    )


def _envelope(
    *,
    mode: str,
    job_id: str,
    view: Mapping[str, Any],
    target: Mapping[str, Any],
    texts: Sequence[str],
    session: Optional[str],
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """One execution report: the standard fields, plus what this mode adds.

    ``next`` is spelled out rather than implied, because the resume command is what a caller who
    lost the channel needs most and should not have to rebuild from offsets by hand.
    """
    offsets = dict(view.get("offsets") or {"stdout": 0, "stderr": 0})
    payload: Dict[str, Any] = {
        "mode": mode,
        "job_id": job_id,
        "stream_model": STREAM_MODEL,
        "protocol": PROTOCOL_VERSION,
        "target": dict(target),
        "session": session,
        **{key: value for key, value in dict(view).items() if key != "offsets"},
        "offsets": offsets,
        "next": _resume_hint(job_id, view.get("exec_id"), offsets),
    }
    if len(texts) == 1:
        payload["command"] = texts[0]
    elif texts:
        payload["commands"] = list(texts)
    if extra:
        payload.update(extra)
    return payload


def _echo_view(view: Mapping[str, Any], *, truncated: bool = False) -> None:
    """Print a report's bodies in human mode: stdout to stdout, stderr to stderr."""
    stdout = str(view.get("stdout") or "")
    stderr = str(view.get("stderr") or "")
    if stdout:
        click.echo(stdout, nl=not stdout.endswith("\n"))
    if stderr:
        click.echo(stderr, nl=not stderr.endswith("\n"), err=True)
    if truncated:
        click.secho(
            f"[output capped at {DEFAULT_MAX_BYTES} bytes per stream; `offsets` are the full "
            "counts - re-read with --attach --after <offsets>]",
            fg="yellow",
            err=True,
        )


# ---------------------------------------------------------------------------
# The modes that run or watch an exec
# ---------------------------------------------------------------------------


def _dispatch(
    ctx: Context,
    client: Any,
    prefixed: Sequence[str],
    *,
    session: Optional[str],
    session_bootstrap: Optional[str],
) -> List[Dict[str, Any]]:
    """Start the exec and return its record(s). A dispatch failure has already exited."""
    exec_id = new_exec_id()
    payload = _observe(
        ctx,
        lambda: client.run(
            prefixed[0] if len(prefixed) == 1 else list(prefixed),
            exec_id=exec_id,
            session_name=session,
            **({"session_bootstrap": session_bootstrap} if session_bootstrap is not None else {}),
        ),
        exec_id=exec_id,
    )
    records = _records_of(payload)
    if not records:
        _handle_error(
            ctx,
            "ExecdError",
            "execd accepted the dispatch but returned no execution record",
            EXIT_API_ERROR,
            hint=(
                f"the command may have started anyway: inspect {exec_id} with `--status` or "
                "`--attach` before re-sending it, because a lost answer is not a command that did "
                "not run."
            ),
            data={"exec_id": exec_id},
        )
    return records


def _watch(
    ctx: Context,
    client: Any,
    records: Sequence[Mapping[str, Any]],
    *,
    job_id: str,
    timeout: Optional[float],
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Follow each exec to a terminal state. Returns (reports, stop cause).

    The stop cause is ``timeout`` or ``interrupted``: both mean the local watcher gave up while the
    exec kept running, and both are reported with the id and the offsets reached rather than as a
    verdict on the command. Later commands in a batch are left unwatched for the same reason -
    watching them would burn another budget on a run the caller has already walked away from. With no
    budget named there is nothing to run out, so the only stop causes left are the ones the pod answers.

    The platform closes a job channel on a lifetime of its own, well inside a long command. That death
    is the follow's to absorb and the caller's to hear about: `on_reconnect` says the one line, and the
    read carries on from the byte it last delivered.
    """
    reports: List[Dict[str, Any]] = []
    stopped: Optional[str] = None
    for record in records:
        exec_id = str(_field(record, "exec_id", "id", default="") or "")
        own = _Streams(live=not ctx.json_output)
        final: Mapping[str, Any] = dict(record)
        chunked = False
        # The dispatch answer is the record's state, not its output, so a record that already looks
        # finished is followed too: its pages are where stdout and stderr are. A fast command that
        # ended before the answer arrived would otherwise report no output at all.
        if exec_id:
            answer: Any = None
            try:
                answer = client.follow(
                    exec_id,
                    timeout=timeout,
                    on_output=own.on_output,
                    on_reconnect=_channel_notice(),
                )
            except KeyboardInterrupt:
                # Ctrl-C detaches, it does not cancel: what the exec was doing (a training run, an
                # install) is the caller's business, and interrupting it is `--cancel`'s, as a call
                # you have to mean. The record stays exactly where the pod left it.
                stopped = "interrupted"
                final = _best_effort_status(client, exec_id, final)
                reports.append(_report(exec_id, final, own, interrupted=True))
                break
            except JobReadinessTimeoutError as exc:
                # The channel died and there was no job left to reopen one on. The exec this watch was
                # reading may well have finished by now; this process cannot say so, and 14 is the
                # difference between "the pod was not there" and "the answer was lost on the way".
                _report_readiness_timeout(ctx, exc, exec_id=exec_id)
            except ExecdError as exc:
                # Past the follow's own reconnects. `job_id` is what lets the report spell the whole
                # resume command, offsets included, instead of naming a flag to add by hand.
                _report_execd_error(ctx, exc, exec_id=exec_id, job_id=job_id)
            else:
                final = _final_record(ctx, client, exec_id, _as_mapping(answer))
                chunked = own.echoed or bool(own.text("stdout") or own.text("stderr"))
                if not _is_terminal(_state_of(final)):
                    stopped = "timeout"
        reports.append(_report(exec_id, final, own, chunked=chunked))
        if stopped:
            break
    return reports, stopped


def _final_record(
    ctx: Context, client: Any, exec_id: str, hint: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """The record to report a finished follow with.

    A follow may answer with the final record or with chunks only; when it answered with neither,
    the status read is asked, because "we stopped watching" must never come back as "it succeeded".
    """
    if hint and _state_of(hint):
        return dict(hint)
    observed = _as_mapping(_observe(ctx, lambda: client.status(exec_id), exec_id=exec_id))
    if observed and _state_of(observed):
        return observed
    return {"exec_id": exec_id, "state": "unobserved"}


def _best_effort_status(client: Any, exec_id: str, fallback: Mapping[str, Any]) -> Dict[str, Any]:
    """One status read after an interrupt, and no error if it fails.

    The follow has already given up; a failed read here must not turn a clean detach into a second
    failure, because the useful thing to report is the id and the offsets reached.
    """
    try:
        observed = _as_mapping(client.status(exec_id))
    except Exception:  # noqa: BLE001 - an interrupted watcher reports the id, not the read error
        return dict(fallback)
    if observed and _state_of(observed):
        return observed
    return dict(fallback)


def _report(
    exec_id: str,
    record: Mapping[str, Any],
    streams: _Streams,
    *,
    interrupted: bool = False,
    chunked: bool = False,
    watched: bool = True,
) -> Dict[str, Any]:
    """One exec's report.

    ``timed_out`` is only ever set for a run we *watched* and could not see finish: a detached exec
    is non-terminal by request, and calling that a timeout would turn the option's success into an
    alarm.
    """
    view = _exec_view(exec_id, record, streams)
    timed_out = watched and not interrupted and not _is_terminal(_state_of(record))
    if timed_out:
        view["timed_out"] = True
    if interrupted:
        view["interrupted"] = True
    return {"view": view, "streams": streams, "chunked": chunked, "record": dict(record)}


def _emit_watches(
    ctx: Context,
    mode: str,
    job_id: str,
    reports: Sequence[Mapping[str, Any]],
    *,
    target: Mapping[str, Any],
    texts: Sequence[str],
    session: Optional[str],
    stopped: Optional[str],
) -> None:
    """One JSON document (or one human block per exec), then the exit code is the caller's job.

    A batch answers with one envelope holding every exec under ``results`` and the last one's fields
    at the top, so a single-command caller and a batch caller read the same keys.
    """
    if not reports:
        return
    views = [dict(report["view"]) for report in reports]
    extra: Dict[str, Any] = {}
    if stopped:
        extra["stopped"] = stopped
    if len(views) > 1:
        extra["results"] = views
    payload = _envelope(
        mode=mode,
        job_id=job_id,
        view=views[-1],
        target=target,
        texts=texts,
        session=session,
        extra=extra,
    )
    if ctx.json_output:
        click.echo(_dump(payload))
        return
    for report, view in zip(reports, views, strict=True):
        if not report["chunked"] and not report["streams"].echoed:
            _echo_view(view, truncated=bool(view.get("truncated")))
        click.secho(_human_exec_line(view), dim=True, err=True)
        if view.get("timed_out") or view.get("interrupted"):
            offsets = view.get("offsets") or {}
            click.secho(
                "the exec is untouched - keep watching with `inspire job exec "
                f"{job_id} --attach {view.get('exec_id')} --after "
                f"{offsets.get('stdout', 0)}:{offsets.get('stderr', 0)} --follow`",
                fg="yellow",
                err=True,
            )
    if stopped == "output_incomplete":
        offsets = views[-1].get("offsets") or {}
        click.secho(
            "the exec is terminal but its output could not be drained completely; resume with "
            f"`inspire job exec {job_id} --attach {views[-1].get('exec_id')} --after "
            f"{offsets.get('stdout', 0)}:{offsets.get('stderr', 0)} --follow`",
            fg="yellow",
            err=True,
        )


def _exit_from(reports: Sequence[Mapping[str, Any]], stopped: Optional[str]) -> None:
    """Exit with the command's own code, or with the reason the watcher stopped.

    A local timeout outranks a command failure (an unknown outcome is the more urgent answer), and
    an interrupt reports itself rather than the code of a command we never saw end.
    """
    if stopped == "timeout":
        raise SystemExit(EXIT_LOCAL_TIMEOUT)
    if stopped == "interrupted":
        raise SystemExit(EXIT_INTERRUPTED)
    if stopped == "output_incomplete":
        raise SystemExit(EXIT_API_ERROR)
    if not reports:
        raise SystemExit(EXIT_API_ERROR)
    record = reports[-1].get("record") or {}
    code = _exit_code_of(record)
    if code is not None:
        raise SystemExit(int(code))
    state = _state_of(record)
    if state and not _is_terminal(state):
        raise SystemExit(EXIT_LOCAL_TIMEOUT)
    # Terminal with no code (`outcome_uncertain`, a pod that died mid-run): the honest answer is
    # non-zero, and not a number this CLI invented.
    raise SystemExit(EXIT_API_ERROR)


def _mode_run(
    ctx: Context,
    client: Any,
    job_id: str,
    texts: Sequence[str],
    prefixed: Sequence[str],
    call: _Invocation,
    target: Mapping[str, Any],
) -> None:
    records = _dispatch(
        ctx,
        client,
        prefixed,
        session=call.session,
        session_bootstrap=call.session_bootstrap,
    )
    reports, stopped = _watch(ctx, client, records, job_id=job_id, timeout=call.timeout)
    _emit_watches(
        ctx,
        "run",
        job_id,
        reports,
        target=target,
        texts=texts,
        session=call.session,
        stopped=stopped,
    )
    _exit_from(reports, stopped)


def _mode_detach(
    ctx: Context,
    client: Any,
    job_id: str,
    texts: Sequence[str],
    prefixed: Sequence[str],
    call: _Invocation,
    target: Mapping[str, Any],
) -> None:
    """Start the exec and report the id. Nothing here waits, so nothing here can time out."""
    records = _dispatch(
        ctx,
        client,
        prefixed,
        session=call.session,
        session_bootstrap=call.session_bootstrap,
    )
    reports = [
        _report(
            str(_field(record, "exec_id", "id", default="") or ""),
            record,
            _Streams(live=False),
            watched=False,
        )
        for record in records
    ]
    _emit_watches(
        ctx,
        "detach",
        job_id,
        reports,
        target=target,
        texts=texts,
        session=call.session,
        stopped=None,
    )
    if not ctx.json_output and len(reports) == 1:
        exec_id = reports[0]["view"].get("exec_id")
        click.secho(
            f"Detached: this process exiting does not touch the exec. Follow it with "
            f"`inspire job exec {job_id} --attach {exec_id} --follow`.",
            dim=True,
            err=True,
        )


def _output_drained(record: Mapping[str, Any]) -> bool:
    """Whether the pod has said both logs were read to their end.

    Only an explicit ``False`` counts against it. An answer that says nothing keeps the reading it had
    before the flag existed - a watch that ran out is reported as a watch that ran out by ``state``,
    and inventing an incomplete-output verdict from a silent field would do it twice.
    """
    return not any(_window_eof(record, stream) is False for stream in ("stdout", "stderr"))


def _mode_follow(
    ctx: Context,
    client: Any,
    job_id: str,
    exec_id: str,
    call: _Invocation,
    target: Mapping[str, Any],
) -> None:
    """Watch an exec that is already on the pod, from the byte an earlier read stopped at.

    This is the command the CLI prints when a watch ends early, so it has to be as durable as the watch
    that printed it: the read goes through the client's own ``follow``, which reopens a channel the
    platform closed and asks again from the byte each stream was last delivered, saying the one line out
    loud. Reading windows here instead would hand the caller a remedy weaker than the failure it answers.

    ``--after`` is handed to the follow as its starting offsets, not read out first and thrown away: the
    bytes behind them are the ones the caller has already seen, and a resume that showed them again
    would not be a resume.
    """
    streams = _Streams(live=not ctx.json_output, cap=max(call.max_bytes, 1))
    # The counters start at the requested offsets: `offsets` in a report is an absolute position in the
    # pod's log, so a resumed read has to report where it ended up, not how much it fetched.
    streams.offsets = {"stdout": 0, "stderr": 0, **call.after_offsets}
    stopped: Optional[str] = None
    try:
        answer = client.follow(
            exec_id,
            timeout=call.timeout,
            from_offsets=dict(call.after_offsets),
            max_bytes=call.max_bytes,
            on_output=streams.on_output,
            on_reconnect=_channel_notice(),
        )
    except KeyboardInterrupt:
        # Ctrl-C detaches this watcher and touches nothing else: what the exec was doing is the
        # caller's business, and interrupting it is `--cancel`'s, as a call you have to mean. One last
        # read gives the report a state; a failed read leaves the id standing in for one.
        final = _best_effort_status(client, exec_id, {"exec_id": exec_id})
        stopped = "interrupted"
    except JobReadinessTimeoutError as exc:
        # The channel died and there was no job left to reopen one on. The exec this watch was reading
        # may well have finished by now; this process cannot say so, and 14 is the difference between
        # "the pod was not there" and "the answer was lost on the way".
        _report_readiness_timeout(ctx, exc, exec_id=exec_id)
    except ExecdError as exc:
        # Past the follow's own reconnects: the resume command is spelled out with the offsets this
        # reader stopped at, so the give-up is one short call away from being a resumed read.
        _report_execd_error(ctx, exc, exec_id=exec_id, job_id=job_id)
    else:
        final = _final_record(ctx, client, exec_id, _as_mapping(answer))
        if not _is_terminal(_state_of(final)):
            stopped = "timeout"
        elif not _output_drained(final):
            # Terminal is the record's last state, not its last byte. A stream that never reported eof
            # leaves output on the pod, and calling that a clean read would be this report's own
            # invention - the offsets it carries are where the next read picks the rest up.
            stopped = "output_incomplete"
    chunked = streams.echoed or bool(streams.text("stdout") or streams.text("stderr"))
    reports = [
        _report(exec_id, final, streams, chunked=chunked, interrupted=stopped == "interrupted")
    ]
    _emit_watches(
        ctx, "attach", job_id, reports, target=target, texts=(), session=None, stopped=stopped
    )
    _exit_from(reports, stopped)


def _mode_attach(
    ctx: Context,
    client: Any,
    job_id: str,
    call: _Invocation,
    target: Mapping[str, Any],
) -> None:
    """Read one window of an exec's two logs, or - with ``--follow`` - watch it to its end.

    The window honors ``--after`` exactly, and the offsets this reports are the numbers to hand to the
    next call. Without ``--follow`` this is one read and nothing more: a single ``--attach`` that meets
    a dead channel answers with that channel, which is what it saw, and does not pretend to a watch.
    """
    exec_id = str(call.exec_id)
    if call.follow:
        _mode_follow(ctx, client, job_id, exec_id, call, target)
        return
    window = _observe(
        ctx,
        lambda: client.attach(
            exec_id,
            stdout_offset=call.after_offsets["stdout"],
            stderr_offset=call.after_offsets["stderr"],
            max_bytes=call.max_bytes,
        ),
        exec_id=exec_id,
    )
    record = _as_mapping(window) or {"exec_id": exec_id}
    streams = _Streams(live=False, cap=max(call.max_bytes, 1))
    # Start the counters at the requested offsets: `offsets` in a report is an absolute position in
    # the pod's log, so a resumed read has to report where it ended up, not how much it fetched.
    streams.offsets = {"stdout": 0, "stderr": 0, **call.after_offsets}
    fetched: Dict[str, int] = {"stdout": 0, "stderr": 0}
    for stream in ("stdout", "stderr"):
        body = _body_of(record, stream)
        counted = _window_bytes(record, stream)
        fetched[stream] = len(body.encode("utf-8")) if counted is None else counted
        streams.adopt(body, stream)
        declared = _window_next(record, stream)
        if declared is not None:
            streams.offsets[stream] = declared

    view = _exec_view(exec_id, record, streams)
    payload = _envelope(
        mode="attach",
        job_id=job_id,
        view=view,
        target=target,
        texts=(),
        session=None,
        extra={
            "after": dict(call.after_offsets),
            "more": _has_more(record, fetched, call.max_bytes),
        },
    )
    if ctx.json_output:
        click.echo(_dump(payload))
        return
    _echo_view(view, truncated=bool(view.get("truncated")))
    click.secho(_human_exec_line(payload), dim=True, err=True)
    if payload["more"]["stdout"] or payload["more"]["stderr"]:
        click.secho(
            "More output is on the pod than this call fetched: re-read with "
            f"`inspire job exec {job_id} --attach {exec_id} --after "
            f"{view['offsets']['stdout']}:{view['offsets']['stderr']}`, or raise --max-bytes.",
            dim=True,
            err=True,
        )


def _has_more(record: Mapping[str, Any], fetched: Mapping[str, int], cap: int) -> Dict[str, bool]:
    """Whether the pod holds bytes past the window this call fetched.

    A full window and a finished log look identical from a byte count, and only ``more: false``
    makes an attach safe to treat as complete - so an answer that does not say is given the
    conservative reading, "a window that filled the cap may have more behind it".
    """
    flags = record.get("more")
    if isinstance(flags, Mapping):
        return {
            "stdout": bool(flags.get("stdout", True)),
            "stderr": bool(flags.get("stderr", True)),
        }
    if "stdout_more" in record or "stderr_more" in record:
        return {
            "stdout": bool(record.get("stdout_more", True)),
            "stderr": bool(record.get("stderr_more", True)),
        }
    limit = max(cap, 1)
    more = {
        "stdout": int(fetched.get("stdout", 0)) >= limit,
        "stderr": int(fetched.get("stderr", 0)) >= limit,
    }
    # A window that says the stream is finished settles the question however full it was, and one that
    # says it is not settles it the other way; only a stream that says nothing keeps the conservative
    # "a window that filled the cap may have more behind it" reading.
    for stream in ("stdout", "stderr"):
        window = _window_of(record, stream)
        if window is not None and "eof" in window:
            more[stream] = not bool(window["eof"])
    return more


def _mode_status(
    ctx: Context, client: Any, job_id: str, call: _Invocation, target: Mapping[str, Any]
) -> None:
    """Report one record. The process exit code answers "did the read work", never the exec's.

    The recorded exit code belongs to a command this call did not run; mirroring it here would make
    a successful read of a failed run look like a failed read, and `--json` already carries it.
    """
    exec_id = str(call.exec_id)
    record = _as_mapping(_observe(ctx, lambda: client.status(exec_id), exec_id=exec_id))
    if not record:
        record = {"exec_id": exec_id, "state": "unobserved"}
    view = _exec_view(exec_id, record, None)
    payload = _envelope(
        mode="status", job_id=job_id, view=view, target=target, texts=(), session=None
    )
    if ctx.json_output:
        click.echo(_dump(payload))
    else:
        _echo_view(view)
        click.secho(_human_exec_line(payload), dim=True, err=True)
    if payload["state"] in ("unknown", "unobserved"):
        click.secho(
            "We could not observe this record. That is neither 'finished' nor 'failed': the id may "
            "belong to another pod, or the record was already cleaned up.",
            fg="yellow",
            err=True,
        )


def _mode_list(ctx: Context, client: Any, job_id: str, target: Mapping[str, Any]) -> None:
    response = _observe(ctx, lambda: client.list_records())
    items = _records_of(response)
    payload: Dict[str, Any] = {
        "mode": "list",
        "job_id": job_id,
        "stream_model": STREAM_MODEL,
        "protocol": PROTOCOL_VERSION,
        "target": dict(target),
        "items": [
            {
                "exec_id": str(_field(item, "exec_id", "id", default="") or ""),
                "state": _state_of(item) or "unknown",
                "exit_code": _exit_code_of(item),
                "offsets": _offsets_of(item),
                "session": _field(item, "session", "session_name"),
                "command_count": _field(item, "command_count"),
                "payload_sha256": _field(item, "payload_sha256"),
            }
            for item in items
        ],
        "total": len(items),
    }
    if ctx.json_output:
        click.echo(_dump(payload))
        return
    for item in payload["items"]:
        code = item["exit_code"]
        click.echo(
            f"{item['exec_id']}  {item['state']}  "
            f"{'exit ' + str(code) if code is not None else 'no exit code'}  "
            f"stdout {item['offsets']['stdout']}B / stderr {item['offsets']['stderr']}B"
            + (f"  [{item['session']}]" if item.get("session") else "")
        )
    if not items:
        click.echo("no exec records on this pod")


def _mode_action(
    ctx: Context,
    client: Any,
    job_id: str,
    call: _Invocation,
    target: Mapping[str, Any],
    *,
    verb: str,
) -> None:
    """``--cancel`` and ``--cleanup``: ask the pod to act, then report its answer."""
    exec_id = str(call.exec_id)
    outcome = _observe(
        ctx,
        lambda: client.cancel(exec_id) if verb == "cancel" else client.cleanup(exec_id),
        exec_id=exec_id,
    )
    record = _as_mapping(outcome) or {}
    if verb == "cleanup" and record.get("complete") is False:
        _handle_error(
            ctx,
            "ExecdError",
            f"execd did not completely remove {exec_id}",
            EXIT_API_ERROR,
            hint="inspect the observed files and retry cleanup; do not remove pod files by hand",
            data={"exec_id": exec_id, "observed": record},
        )
    if verb == "cancel":
        view = _exec_view(exec_id, record, None)
        payload = _envelope(
            mode="cancel", job_id=job_id, view=view, target=target, texts=(), session=None
        )
    else:
        payload = {
            "mode": "cleanup",
            "job_id": job_id,
            "exec_id": exec_id,
            "cleaned": True,
            "stream_model": STREAM_MODEL,
            "protocol": PROTOCOL_VERSION,
            "target": dict(target),
        }
    if record:
        payload["observed"] = record
    if ctx.json_output:
        click.echo(_dump(payload))
        return
    if verb == "cancel":
        delivered = record.get("delivered")
        delivery = (
            "  signal delivered"
            if delivered is True
            else "  no signal delivered" if delivered is False else ""
        )
        click.echo(
            f"cancel requested for {exec_id}  state {payload.get('state') or 'unknown'}"
            + delivery
            + (f"  exit {payload['exit_code']}" if payload.get("exit_code") is not None else "")
        )
        click.secho(
            f"The record stays on the pod: `--status {exec_id}` reads what it printed, "
            f"`--cleanup {exec_id}` removes it.",
            dim=True,
            err=True,
        )
    else:
        click.echo(f"cleaned {exec_id}")


def _mode_daemon(ctx: Context, client: Any, job_id: str, target: Mapping[str, Any]) -> None:
    health = _observe(ctx, lambda: client.health())
    record = _as_mapping(health) or {"state": str(health)}
    payload = {
        "mode": "daemon",
        "job_id": job_id,
        "stream_model": STREAM_MODEL,
        "protocol": PROTOCOL_VERSION,
        "target": dict(target),
        "daemon": record,
        "reachable": True,
    }
    if ctx.json_output:
        click.echo(_dump(payload))
        return
    pod = str(target.get("instance") or f"worker {target.get('worker')}")
    click.echo(f"execd on {pod}: " + ", ".join(f"{key}={value}" for key, value in record.items()))


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@click.command("exec", context_settings=dict(ignore_unknown_options=True))
@click.argument("job_id")
@click.argument("command", nargs=-1, required=False, type=click.UNPROCESSED)
@click.option("--detach", is_flag=True, help="Start the exec and return as soon as it is accepted.")
@click.option(
    "--attach",
    "attach_id",
    metavar="EXEC_ID",
    help="Read one exec's output from its record: stdout to stdout, stderr to stderr. Read-only "
    "unless --follow is given.",
)
@click.option(
    "--after",
    metavar="STDOUT:STDERR",
    default="0:0",
    show_default=True,
    help="With --attach: byte offsets to resume from, exactly as an earlier report printed them "
    "under `offsets`.",
)
@click.option(
    "--follow",
    is_flag=True,
    help="With --attach: keep watching until the exec is terminal, or until --timeout expires when "
    "one was given. Like any watch, this one survives a channel the platform closes and says so once.",
)
@click.option(
    "--status",
    "status_id",
    metavar="EXEC_ID",
    help="Report one exec's state, exit code and offsets. Read-only.",
)
@click.option("--list", "list_records", is_flag=True, help="List this pod's exec records.")
@click.option(
    "--cancel",
    "cancel_id",
    metavar="EXEC_ID",
    help="Ask the pod to stop one exec. The record stays, so --status still explains the run.",
)
@click.option("--cleanup", "cleanup_id", metavar="EXEC_ID", help="Remove one exec's record.")
@click.option(
    "--daemon-status",
    is_flag=True,
    help="Ask the pod-side execd for its own health. Read-only, and needs no command.",
)
@click.option(
    "--stdin",
    is_flag=True,
    help="Read the command from stdin, verbatim - no shell quoting, no PTY line limit.",
)
@click.option(
    "-c",
    "--command",
    "commands",
    multiple=True,
    metavar="CMD",
    help="Run CMD, then the next -c, as one batch on the pod. Repeat it per command; the reported "
    "exit code is the last non-zero command's, or zero when every command succeeds.",
)
@click.option(
    "--session",
    metavar="NAME",
    help="Run in a named pod-side session, so cwd and exports carry into a later call. With "
    "--workers each pod keeps its own session under that name.",
)
@click.option("--worker", type=int, default=0, show_default=True, help="Worker index to target.")
@click.option(
    "--attempt",
    type=int,
    help="Fault-tolerance attempt for --worker; defaults to the newest (the reserved pod that just "
    "failed is the newest, not the first).",
)
@click.option("--instance", "instance_name", help="Exact platform instance/pod name.")
@click.option(
    "--workers",
    "workers_spec",
    metavar="SPEC",
    help="Run the command on several workers of one job: 'all' or indices like '0,3-5'. Each pod "
    "gets its own execd exec, so every pod's line has its own stdout, stderr and exit code.",
)
@click.option(
    "--parallel",
    type=int,
    default=DEFAULT_FANOUT_PARALLEL,
    show_default=True,
    help="With --workers: how many pods may hold a command session at once.",
)
@click.option(
    "--summary-only",
    is_flag=True,
    help="With --workers: per-pod verdicts, exit codes and durations, no output bodies.",
)
@click.option(
    "--timeout",
    type=float,
    default=None,
    help="Bound, in seconds, on how long THIS process watches; the exec is never bounded and there is "
    "no default. Leave it out and the exec is followed to its terminal state and its own exit code "
    "is returned - a page at a time, so no single read is held open past what the channel tolerates. "
    "Give it and expiry ends this call with 124, the exec untouched, carrying the exec_id and the "
    "offsets to resume from; a non-positive value is refused.",
)
@click.option(
    "--wait-timeout",
    type=int,
    default=0,
    show_default=True,
    help="Seconds to wait for the job to have a runnable instance before anything is sent. 0 fails "
    "immediately. Nothing here bounds the command: --timeout bounds how long this process watches "
    "it, and only --cancel stops it.",
)
@click.option(
    "--max-bytes",
    type=int,
    default=DEFAULT_MAX_BYTES,
    show_default=True,
    help="With --attach/--follow/--workers: bytes of each stream to read or capture per call.",
)
@json_option
@pass_context
def exec_job(
    ctx: Context,
    job_id: str,
    command: tuple[str, ...],
    detach: bool,
    attach_id: str | None,
    after: str,
    follow: bool,
    status_id: str | None,
    list_records: bool,
    cancel_id: str | None,
    cleanup_id: str | None,
    daemon_status: bool,
    stdin: bool,
    session: str | None,
    worker: int,
    attempt: int | None,
    instance_name: str | None,
    workers_spec: str | None,
    parallel: int,
    summary_only: bool,
    timeout: float | None,
    wait_timeout: int,
    max_bytes: int,
    commands: tuple[str, ...] = (),
    json_output: bool = False,
) -> None:
    """Execute a command in a running training job container through the pod-side execd.

    \b
    By default the exec is started and followed to its terminal state, and its own exit code comes
    back - however long the command takes:
        inspire job exec job-... -- nvidia-smi

    \b
    --timeout is a caller's own stopping point, not a leash on the command. Give it when you would
    rather come back for the rest:
        inspire job exec job-... --timeout 30 -- python train.py --epochs 90

    \b
    Or start it and walk away; either way the exec outlives this process:
        inspire job exec job-... --detach -- python train.py --epochs 90
        inspire job exec job-... --attach ex-0123... --after 0:0 --follow

    \b
    Ctrl-C, and --timeout when you gave one, stop the watching, never the exec. Both exit carrying its
    exec_id and the byte offsets reached (130 and 124), and --attach resumes from them. A --workers
    sweep carries one of each per pod. Interrupting the command itself is --cancel, a separate call
    you have to mean.

    \b
    The read-only verbs run no command, so they load no runtime environment either - no [remote_env],
    no venv, no cd - and cannot echo a configured secret:
        inspire job exec job-... --daemon-status
        inspire job exec job-... --list
        inspire job exec job-... --status ex-0123...
        inspire job exec job-... --cleanup ex-0123...

    \b
    A batch shares one pod-side execution, a session keeps shell state across calls, and a fan-out
    compares pods:
        inspire job exec job-... --session probe -c 'cd /work/run1' -c 'nvidia-smi'
        inspire job exec job-... --workers 0,3-5 --summary-only -- nvidia-smi

    \b
    stdout and stderr stay separate in every mode. --json reports them under `stdout`/`stderr` with
    `exec_id`, `state`, `exit_code`, `offsets` and `stream_model`; the process exits with the
    command's own code, 124 when a --timeout you gave ran out, or 130 when Ctrl-C stopped the watcher.
    """
    json_output = resolve_json_output(ctx, json_output)
    call = _Invocation(
        # Click knows which options this command line named, which is the only way to tell "the
        # caller did not mention this" from "the caller named the default".
        given=_supplied_labels(),
        command=command,
        commands=commands,
        stdin=stdin,
        detach=detach,
        attach_id=attach_id,
        status_id=status_id,
        cancel_id=cancel_id,
        cleanup_id=cleanup_id,
        list_records=list_records,
        daemon_status=daemon_status,
        follow=follow,
        after=after,
        max_bytes=max_bytes,
        session=session,
        worker=worker,
        attempt=attempt,
        instance_name=instance_name,
        workers_spec=workers_spec,
        parallel=parallel,
        summary_only=summary_only,
        timeout=timeout,
        wait_timeout=wait_timeout,
    )
    if not _resolve_mode(ctx, call):
        return
    if not _reject_unusable_options(ctx, call) or not _validate_numbers(ctx, call):
        return

    texts: List[str] = []
    prefixed: List[str] = []
    prefix: List[str] = []
    if call.mode in ("run", "detach"):
        collected = _collect_command_texts(ctx, call)
        if collected is None:
            return
        texts = collected
        # Only a command needs the runtime environment, and only the payload it is sent with carries
        # it: what is echoed, and what lands in the report under `command`, stays the caller's text.
        loaded = _runtime_prefix(ctx)
        if loaded is None:
            return
        prefix = loaded
        if call.session is not None:
            call.session_bootstrap = " && ".join(prefix) if prefix else None
            prefixed = list(texts)
        else:
            prefixed = [_compose_runtime_command(text, prefix) for text in texts]

    job_id = resolve_job_id(ctx, job_id)
    target: Dict[str, Any] = {"worker": worker, "attempt": attempt, "instance": instance_name}

    if call.fanout:
        _run_fanout(ctx, job_id, texts, prefixed, call)
        return

    # The client owns one command session on the pod; closing it releases that channel, while the
    # execd daemon, its records and any running exec outlive it - which is what makes --detach mean
    # what it says.
    with _observe(
        ctx,
        lambda: _open_client(
            job_id,
            worker=worker,
            attempt=attempt,
            instance_name=instance_name,
            wait_timeout=wait_timeout,
        ),
    ) as client:
        mode = call.mode
        if mode == "run":
            _mode_run(ctx, client, job_id, texts, prefixed, call, target)
        elif mode == "detach":
            _mode_detach(ctx, client, job_id, texts, prefixed, call, target)
        elif mode == "attach":
            _mode_attach(ctx, client, job_id, call, target)
        elif mode == "status":
            _mode_status(ctx, client, job_id, call, target)
        elif mode == "list":
            _mode_list(ctx, client, job_id, target)
        elif mode in ("cancel", "cleanup"):
            _mode_action(ctx, client, job_id, call, target, verb=mode)
        elif mode == "daemon":
            _mode_daemon(ctx, client, job_id, target)


def _fanout_rows(job_id: str) -> tuple[List[Dict[str, Any]], Optional[int]]:
    """The newest attempt's instance rows, and the attempt number they belong to.

    The single-pod paths resolve the latest attempt per worker; a sweep applies the same rule once for
    the whole sweep, because one that mixed attempts would answer "what is different about worker 3"
    with a comparison against a dead predecessor of worker 0.
    """
    items = _browser_api.list_job_instances(job_id, page_size=-1).get("items") or []
    rows = [item for item in items if isinstance(item, dict)]
    if not rows:
        raise ValueError(f"No instances found for job {job_id}")
    described = [describe_instance(row) for row in rows]
    attempts: List[int] = [
        item["attempt"] for item in described if isinstance(item.get("attempt"), int)
    ]
    if not attempts:
        # No recognisable pod names at all: nothing to filter by, so every row is the sweep.
        return rows, None
    newest = max(attempts)
    return (
        [row for row, item in zip(rows, described, strict=True) if item.get("attempt") == newest],
        newest,
    )


def _fanout_record(
    worker: Optional[int],
    row: Optional[Mapping[str, Any]],
    newest_attempt: Optional[int],
    status: str,
    **extra: Any,
) -> Dict[str, Any]:
    """One pod's line in the sweep report, identity filled in even when the pod answered nothing.

    A sweep is read as a table, so who-the-pod-is is always present and what-happened is whatever this
    call got round to learning; a row missing its worker index would be worse than one that says
    "no answer".
    """
    described = describe_instance(dict(row)) if row is not None else {}
    record: Dict[str, Any] = {
        "worker": worker,
        "attempt": described.get("attempt", newest_attempt),
        "instance": described.get("name"),
        "node": described.get("node"),
        "kind": described.get("kind"),
        "status": status,
        "state": None,
        "exec_id": None,
        "exit_code": None,
        "returncode": None,
        "stream_model": STREAM_MODEL,
        "offsets": {"stdout": 0, "stderr": 0},
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "commands": [],
        "duration_ms": None,
        "error": None,
    }
    record.update(extra)
    return record


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``, for a report that says how long a pod took."""
    return int((time.monotonic() - started) * 1000)


def _captured_update(streams: _Streams) -> Dict[str, Any]:
    """What this process read off one pod, in the same fields a single-pod report uses."""
    summary = streams.summary()
    return {
        "stdout": summary["stdout"],
        "stderr": summary["stderr"],
        "truncated": summary["truncated"],
        "offsets": {"stdout": 0, "stderr": 0, **(summary["offsets"] or {})},
    }


def _fanout_pod(
    job_id: str,
    worker: Optional[int],
    row: Mapping[str, Any],
    texts: Sequence[str],
    prefixed: Sequence[str],
    call: _Invocation,
    newest_attempt: Optional[int],
    *,
    position: int,
    max_bytes: int,
) -> Dict[str, Any]:
    """Run the command list on one pod through that pod's execd, and report what it answered.

    Every pod gets its own client, so its own session, its own record and its own two streams: worker
    3's stderr arrives under worker 3's ``stderr`` instead of inside whichever text arrived last. The
    stagger delays only the session open, because N sessions opened in the same instant are a burst
    this gateway answers by dropping some - which would read back here as pods that look unreachable.

    ``--timeout``, when the caller named one, is this process's watching budget for this pod. When it
    runs out the pod's exec keeps running and that pod's line says so with its own ``exec_id`` and
    offsets, which is exactly what a sweep no longer has to sit holding a terminal session open for.
    With no budget named the sweep watches each pod to its own end.
    """
    _sleep(FANOUT_STAGGER_SECONDS * (position % max(call.parallel, 1)))
    record = _fanout_record(worker, row, newest_attempt, "no_answer", commands=list(texts))
    name = str(record.get("instance") or "")
    started = time.monotonic()
    streams = _Streams(live=False, cap=max(max_bytes, 1))
    # The exec id is this call's, not the daemon's, so a dispatch whose answer is lost still has an id
    # to report: the caller can --attach the record this call could not read instead of wondering
    # whether the command ran at all.
    exec_id = new_exec_id()
    dispatched = False
    try:
        with _open_client(
            job_id,
            worker=worker if worker is not None else 0,
            attempt=call.attempt,
            instance_name=name or None,
            wait_timeout=call.wait_timeout,
        ) as client:
            dispatched = True
            records = _records_of(
                client.run(
                    list(prefixed),
                    exec_id=exec_id,
                    session_name=call.session,
                    **(
                        {"session_bootstrap": call.session_bootstrap}
                        if call.session_bootstrap is not None
                        else {}
                    ),
                )
            )
            if not records:
                return {
                    **record,
                    "exec_id": exec_id,
                    "error": "execd accepted the dispatch but returned no execution record",
                    "next": _resume_hint(job_id, exec_id),
                    "duration_ms": _elapsed_ms(started),
                }
            final: Mapping[str, Any] = records[0]
            wanted = str(_field(final, "exec_id", "id", default="") or "") or exec_id
            # The dispatch answer is the record's state, not its output, so the follow runs even when
            # the record already looks finished - its pages are where the two streams are.
            watched = _as_mapping(
                client.follow(
                    wanted,
                    timeout=call.timeout,
                    on_output=streams.on_output,
                    on_reconnect=_channel_notice(
                        label="worker ?" if worker is None else f"worker {worker}"
                    ),
                )
            )
            final = watched or final
            view = _exec_view(wanted, final, streams)
            exit_code = view["exit_code"]
            status = (
                "ok"
                if exit_code == 0
                else ("command_failed" if exit_code is not None else "no_answer")
            )
            item: Dict[str, Any] = {
                **record,
                "status": status,
                "state": view["state"],
                "exec_id": wanted,
                "exit_code": exit_code,
                "returncode": view["returncode"],
                "offsets": view["offsets"],
                "stdout": view["stdout"],
                "stderr": view["stderr"],
                "truncated": view["truncated"],
                "duration_ms": _elapsed_ms(started),
            }
            if status == "no_answer":
                item["next"] = _resume_hint(job_id, wanted, view["offsets"])
            return item
    except ExecdError as exc:
        return _fanout_failure(
            record,
            streams,
            started,
            job_id=job_id,
            dispatched=dispatched,
            exec_id=exec_id,
            error=str(exc),
            code=exc.code,
        )
    except (JobReadinessTimeoutError, ValueError) as exc:
        # Nothing reached a pod - the readiness wait expired, or the call was refused before leaving.
        # No record exists to come back to, so this is "could not start", not "no answer".
        return _fanout_failure(
            record,
            streams,
            started,
            job_id=job_id,
            dispatched=False,
            exec_id=exec_id,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - one pod's lost channel must not drop the other pods
        return _fanout_failure(
            record,
            streams,
            started,
            job_id=job_id,
            dispatched=dispatched,
            exec_id=exec_id,
            error=str(exc),
        )


def _fanout_failure(
    record: Dict[str, Any],
    streams: _Streams,
    started: float,
    *,
    job_id: str,
    dispatched: bool,
    exec_id: str,
    **extra: Any,
) -> Dict[str, Any]:
    """Fold a pod's half-read output into its record, then name what went wrong.

    Bytes that arrived before the channel died are the caller's: a sweep that lost worker 2's answer
    still shows what worker 2 printed and where its log ended, so the follow-up is one ``--attach``
    rather than a re-run of a command that may already have written something. A pod whose session
    never opened is the other finding - nothing was sent, so there is nothing to resume.
    """
    item = {
        **record,
        **_captured_update(streams),
        "status": "no_answer" if dispatched else "unreachable",
        "exec_id": exec_id if dispatched else None,
        "duration_ms": _elapsed_ms(started),
        **extra,
    }
    if dispatched:
        item["next"] = _resume_hint(job_id, exec_id, streams.offsets)
    return item


def _run_fanout(
    ctx: Context, job_id: str, texts: Sequence[str], prefixed: Sequence[str], call: _Invocation
) -> None:
    """Run one command list on every pod of the newest attempt, each through its own execd.

    The sweep is the single-pod path N times over rather than a mode with its own rules: one client
    per pod dispatches, watches and reads that pod's record, so ``stream_model`` says ``separate``
    for worker 5 exactly as it does for ``--worker 0``, and ``--max-bytes`` caps each pod's capture
    instead of the sweep's total. ``--parallel`` bounds how many pods hold a session at once.

    A pod that is not running, and a requested index with no pod at all, are records rather than a
    failed sweep: in the situation a fan-out is run for, "worker 3 was never there" is often the
    difference being looked for. The runtime prefix is the one this call already resolved - loaded
    once, so a sweep cannot half-run under a configuration that changed between two reads of it - and
    every report keeps the caller's own text, because ``[remote_env]`` can hold secrets.
    """
    max_bytes = FANOUT_MAX_BYTES if not call.used("--max-bytes") else call.max_bytes
    try:
        rows, newest_attempt = _fanout_rows(job_id)
    except ValueError as exc:
        # The instance enumeration itself failed (a job with no pods). That is a platform answer
        # about the sweep's targets, not a broken sweep, and it is reported as itself.
        _handle_error(
            ctx,
            "ExecError",
            str(exc),
            EXIT_API_ERROR,
            hint="`inspire --json job instances <job>` lists what the job has right now.",
        )
        return
    except JobReadinessTimeoutError as exc:
        _handle_error(ctx, "Timeout", str(exc), EXIT_TIMEOUT)
        return
    except ExecdError as exc:
        _report_execd_error(ctx, exc)
        return
    except Exception as exc:  # noqa: BLE001 - one platform failure must not print a traceback
        _handle_error(ctx, "ExecError", str(exc), EXIT_API_ERROR)
        return

    by_worker: Dict[Optional[int], Dict[str, Any]] = {}
    for row in rows:
        by_worker.setdefault(describe_instance(row).get("worker_index"), row)
    ordered = sorted(
        by_worker.items(),
        key=lambda pair: (pair[0] is None, pair[0] or 0, str(pair[1].get("name"))),
    )

    records: List[Dict[str, Any]] = []
    if call.requested_workers is None:
        targets = ordered
    else:
        targets = []
        for index in call.requested_workers:
            found = by_worker.get(index)
            if found is None:
                records.append(
                    _fanout_record(
                        index,
                        None,
                        newest_attempt,
                        "absent",
                        error=f"no pod for worker {index} in the newest attempt",
                    )
                )
            else:
                targets.append((index, found))

    runnable: List[tuple[Optional[int], Dict[str, Any]]] = []
    for worker, row in targets:
        if describe_instance(row).get("kind") != "running":
            records.append(
                _fanout_record(
                    worker,
                    row,
                    newest_attempt,
                    "not_schedulable",
                    error="pod is not running; the command was not sent",
                )
            )
        else:
            runnable.append((worker, row))

    with ThreadPoolExecutor(max_workers=max(call.parallel, 1)) as pool:
        futures = [
            pool.submit(
                _fanout_pod,
                job_id,
                worker,
                row,
                texts,
                prefixed,
                call,
                newest_attempt,
                position=position,
                max_bytes=max_bytes,
            )
            for position, (worker, row) in enumerate(runnable)
        ]
        records.extend(future.result() for future in as_completed(futures))

    records.sort(
        key=lambda item: (
            item.get("worker") is None,
            item.get("worker") if item.get("worker") is not None else 0,
            str(item.get("instance") or ""),
        )
    )
    counts = {status: 0 for status in FANOUT_STATUSES}
    for item in records:
        name = str(item.get("status"))
        counts[name] = counts.get(name, 0) + 1
    payload: Dict[str, Any] = {
        "mode": "fanout",
        "job_id": job_id,
        "stream_model": STREAM_MODEL,
        "protocol": PROTOCOL_VERSION,
        "attempt": newest_attempt,
        "workers": "all" if call.requested_workers is None else list(call.requested_workers),
        "parallel": max(call.parallel, 1),
        "max_bytes": max_bytes,
        "session": call.session,
        "items": records,
        "counts": counts,
        "all_ok": all(item.get("status") == "ok" for item in records),
    }
    _emit_fanout(ctx, payload, summary_only=call.summary_only)


def _emit_fanout(ctx: Context, payload: Dict[str, Any], *, summary_only: bool) -> None:
    """One block per pod, then the sweep's verdict, then the exit code is the sweep's answer.

    ``--summary-only`` keeps every verdict and drops the output bodies, on both paths. In text mode a
    pod's stdout goes to stdout and its stderr to stderr, which is what ``stream_model: separate``
    promises here; the per-pod verdict lines and the summary are commentary and go to stderr, so a
    pipe opened on this process's stdout carries the pods' stdout and nothing else.

    The exit code extends the batch's conventions to pods: 124 when a pod answered nothing (an unknown
    outcome outranks a known failure, and that pod's exec may still be running), else the largest
    non-zero command exit code, else non-zero when pods were unreachable, not schedulable or absent.
    """
    items = [dict(item) for item in payload.get("items") or []]
    if summary_only:
        for item in items:
            item.pop("stdout", None)
            item.pop("stderr", None)
    payload = {**payload, "items": items, "summary_only": summary_only}

    if ctx.json_output:
        click.echo(_dump(payload))
    else:
        for item in items:
            worker = item.get("worker")
            label = "worker ?" if worker is None else f"worker {worker}"
            code = item.get("exit_code")
            code_text = "no answer" if code is None else f"exit {code}"
            duration = item.get("duration_ms")
            duration_text = f"  ({duration / 1000:.1f}s)" if isinstance(duration, int) else ""
            click.secho(
                f"{label}  {item.get('instance') or '-'}  {item.get('status')}  "
                f"{code_text}{duration_text}",
                dim=True,
                err=True,
            )
            if not summary_only:
                stdout = str(item.get("stdout") or "")
                if stdout:
                    click.echo(stdout, nl=not stdout.endswith("\n"))
                stderr = str(item.get("stderr") or "")
                if stderr:
                    click.echo(stderr, nl=not stderr.endswith("\n"), err=True)
            if item.get("truncated"):
                click.secho(
                    f"[{label}: output capped at {payload.get('max_bytes')} bytes per stream; "
                    "`offsets` are the full counts - re-read with --attach --after]",
                    fg="yellow",
                    err=True,
                )
            if item.get("error"):
                click.secho(f"[{label}: {item['error']}]", fg="yellow", err=True)
            if item.get("status") == "no_answer" and item.get("exec_id"):
                click.secho(
                    f"[{label}: this process stopped watching; the exec is untouched - keep "
                    f"watching with `{item.get('next')}`]",
                    dim=True,
                    err=True,
                )
        counts = dict(payload.get("counts") or {})
        parts = [f"{counts[status]} {status}" for status in FANOUT_STATUSES if counts.get(status)]
        attempt = payload.get("attempt")
        attempt_text = f"  (attempt {attempt})" if attempt is not None else ""
        click.secho(
            f"fan-out: {', '.join(parts) or 'no workers'} of {len(items)}{attempt_text}",
            dim=True,
            err=True,
        )

    if payload.get("all_ok"):
        return
    statuses = {str(item.get("status")) for item in items}
    if "no_answer" in statuses:
        raise SystemExit(EXIT_LOCAL_TIMEOUT)
    failed_codes = [
        int(item["exit_code"])
        for item in items
        if item.get("status") == "command_failed" and item.get("exit_code") is not None
    ]
    if failed_codes:
        raise SystemExit(max(failed_codes))
    raise SystemExit(EXIT_API_ERROR)


__all__ = ["exec_job"]
