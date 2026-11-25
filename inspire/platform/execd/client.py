"""Local client for a pod-side execd, reached through the platform PTY channel.

What this adds over `job exec --detach`
---------------------------------------
`exec_records.py` answers every question with a fresh shell script, which is correct but pays a command round
trip per question and cannot ask for "the stdout since offset 90210" without re-reading the whole file. execd
keeps each record and its offsets in one daemon inside the pod, so this client asks questions instead of
scripting them: `run` returns the accepted record, `attach` reads a byte range of its two streams, and
`follow` walks one record to a terminal state while streaming whatever it produced.

Two properties drive the design, and both come from measurements on this platform rather than taste:

* **The channel is an observer, never the source of truth.** Instability is concentrated in session
  establishment (one session carried 83 commands while per-command sessions lost the channel all morning), and
  a drop loses the *answer*, not the work. So every method here is safe to repeat: `run` takes its exec id from
  this client rather than from the daemon - which the agent uses to recognise a duplicate and answer with the
  record it already has - and a lost answer is re-asked or re-read rather than re-run. `follow` is where that is
  visible: it reconnects, resumes at the last verified offsets, and does not start the command again.
* **One session for the whole operation.** Establishment is the expensive, flaky step, so the client opens one
  `PlatformWsSession` and keeps it, reconnecting only when it actually broke.

The daemon's long-poll is used rather than a client-side sleep loop: `attach` can wait inside the pod for output
to appear, which costs one exec round trip instead of one per second. On a channel where round trips are the
scarce resource and the failing component, that difference is the design.

Not done here, on purpose
-------------------------
Interrupting a follow is the CLI's job: `KeyboardInterrupt` propagates untouched, so the terminal decides what
Ctrl-C means (keep following, cancel the record, or die) instead of a library guessing. And nothing here falls
back to the legacy exec path when execd is unavailable - that decision belongs in a message a user reads, not in
a silent swap of underlying guarantees.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable

import requests
from websocket import WebSocketException

from inspire.platform.execd import bootstrap
from inspire.platform.execd.bootstrap import (
    DEFAULT_COMMAND_TIMEOUT,
    TRANSPORT_CODES as _TRANSPORT_CODES,
    AgentHandle,
    _redact_error,
    ensure_agent,
    run_rpc,
)
from inspire.platform.execd.protocol import (
    STATES,
    TERMINAL_STATES,
    ExecdError,
    new_exec_id,
    validate_exec_id,
    validate_session,
)
from inspire.platform.web.browser_api import open_job_command_session

#: Seconds between `follow` polls on top of the daemon's own long-poll. The bound is politeness to the exec
#: channel - one PTY round trip per poll - not the daemon's speed.
DEFAULT_FOLLOW_POLL = 0.5

#: Seconds an `attach` asks the daemon to wait for output before answering with nothing. Long-polling in the pod
#: is what keeps a follow at a few round trips instead of one per second.
DEFAULT_FOLLOW_WAIT = 5.0

#: Ceiling on that long-poll, whether or not the caller named a timeout. `bootstrap` widens the shim's socket
#: timeout and the PTY read window to outlast it, so an unbounded watch is still a string of reads each of
#: which ends on its own.
MAX_FOLLOW_WAIT = 30.0

#: Commands per `run`. A batch is one record, not N: commands that must share a shell belong in one record, and
#: splitting them across records would blame the whole batch for the first failure.
MAX_COMMANDS_PER_RUN = 64

#: Attempts per call, including session establishment and bootstrap. More than one is only defensible because
#: every method is safe to repeat - see the module docstring.
DEFAULT_CALL_ATTEMPTS = 6

#: Wait before reopening the session and re-asking, doubled between attempts. The platform has produced
#: failure windows longer than 14 seconds and a historical 4m41s outage; one minute of backoff covers the
#: common short window without pretending an unbounded retry can repair a dead pod.
DEFAULT_RETRY_DELAY = 2.0
MAX_RETRY_DELAY = 30.0

#: What `attach` may ask for per stream in one page. The agent clamps to its own `max_page_bytes`; asking for
#: more is harmless, asking for less is a round trip.
DEFAULT_ATTACH_MAX_BYTES = 65536

#: Consecutive terminal pages that report "not at the end" while carrying no bytes at all, before a follow
#: accepts that there is nothing more to have. Offsets stay wherever they got to, and the result keeps the
#: stream `eof` flags, so stopping here is visible rather than a claim that the output was complete.
MAX_QUIET_DRAINS = 3

#: Consecutive failed reconnects a `follow` spends on a channel that died before it admits the read is over.
#: The platform closes a job channel on a lifetime nobody here controls (330 s measured, see
#: `web/browser_api/ws_exec.py`), and the record outlives it, so one death costs a session and no bytes: what
#: this cap bounds is a pod that stays gone, where every reopen costs another establishment and answers nothing.
#: A reconnect that works resets the count, because three recoveries are not three signs of a dead pod.
DEFAULT_MAX_RECONNECTS = 3

_STREAMS = ("stdout", "stderr")


def _normalize_commands(commands: str | Sequence[str]) -> list[str]:
    """The agent takes a *list* of command strings and refuses a single string, so normalize here.

    A caller who means one command writes `client.run("make")` and gets `["make"]`; a caller who means a batch
    gets one record with several commands, which is the only shape where they share a shell.
    """
    if isinstance(commands, (bytes, bytearray)):
        raise ValueError("commands must be text, not bytes")
    if isinstance(commands, str):
        items: list[Any] = [commands]
    elif isinstance(commands, Sequence):
        items = list(commands)
    else:
        raise ValueError("commands must be a string or a sequence of strings")
    if not items:
        raise ValueError("at least one command is required")
    if len(items) > MAX_COMMANDS_PER_RUN:
        raise ValueError(f"a single exec record takes at most {MAX_COMMANDS_PER_RUN} commands")
    if any(isinstance(item, (bytes, bytearray)) for item in items):
        raise ValueError("commands must be text, not bytes")
    cleaned = [str(item) for item in items]
    if any(not item.strip() for item in cleaned):
        raise ValueError("commands must not contain an empty entry")
    return cleaned


def _with_attempts(exc: ExecdError, attempts: int) -> ExecdError:
    """File the number of attempts this call spent on the error it gives up with.

    A report that only says "we retried" cannot be checked by whoever reads it, and a number the report
    guesses is a lie for any client built with a different ``call_attempts``. The object that looped is the
    one that knows.
    """
    exc.data = {**exc.data, "attempts": attempts}
    return exc


def _stopped_at(
    exc: BaseException, *, record_id: str, offsets: dict[str, int], reconnects: int
) -> ExecdError:
    """The give-up answer: the last transport error, plus where this reader actually stopped.

    The offsets ride along because the caller's next command is built from them - a reader that quits
    without saying which byte it delivered last leaves behind a resume command nobody can write - and the
    count because "the channel stayed down through three reconnects" is a different fact from "one answer
    was missing". Codes and numbers stay verbatim; the message goes through the redactor with the rest.
    """
    data = dict(getattr(exc, "data", None) or {})
    data.update(
        {
            "exec_id": record_id,
            "stdout_offset": int(offsets["stdout"]),
            "stderr_offset": int(offsets["stderr"]),
            "reconnects": int(reconnects),
        }
    )
    if isinstance(exc, ExecdError):
        return ExecdError(exc.code, str(exc), data=data)
    return ExecdError("transport-lost", f"{type(exc).__name__}: {exc}", data=data)


def _check_state(data: dict[str, Any], *, expected_id: str) -> None:
    """Reject an answer that is not about the exec id we asked for, or a state with no meaning.

    The id check is not pedantry: if the daemon answered for a different record, every later call built on this
    one - attach, cancel, cleanup - acts on the wrong run.
    """
    echoed = str(data.get("exec_id") or "")
    if echoed and echoed != expected_id:
        raise ExecdError(
            "protocol-mismatch",
            f"execd answered for exec id {echoed} when {expected_id} was asked",
            data={"requested_exec_id": expected_id, "echoed_exec_id": echoed},
        )
    state = str(data.get("state") or "")
    if state and state not in STATES:
        raise ExecdError(
            "protocol-mismatch",
            f"execd reported an unknown state {state!r} for exec id {expected_id}",
            data={"exec_id": expected_id, "state": state},
        )


def _decode_window(data: dict[str, Any], stream: str, *, fallback_offset: int) -> dict[str, Any]:
    """Turn one agent output page into a window with offsets, bytes and text.

    The agent reports pages flat (`stdout_b64`, `stdout_offset` for the *next* offset, `stdout_eof`); the window
    shape published here is the client's own, because the caller needs three things in one place - where this
    page started, where the next read should ask from, and whether this stream is finished.

    Both `data` (bytes - for streaming callbacks and anything that writes them on) and `text` (decoded with
    replacements - for display) are published: the channel carries whatever a command printed, and decoding
    lossily here would throw away bytes a caller could still have used.
    """
    payload = bootstrap.decode_output(data.get(f"{stream}_b64"))
    declared = data.get(f"{stream}_offset")
    offset = int(fallback_offset)
    next_offset = int(declared) if isinstance(declared, (int, float)) else offset + len(payload)
    if next_offset < offset:
        # A page that claims to end before it started cannot be resumed from, so resume from its end instead of
        # asking forever for an offset that never advances.
        next_offset = offset + len(payload)
    return {
        "offset": offset,
        "next_offset": max(offset, next_offset),
        "bytes": len(payload),
        "eof": bool(data.get(f"{stream}_eof")),
        "data": payload,
        "text": payload.decode("utf-8", errors="replace"),
    }


class JobExecdClient:
    """Ask a job instance's execd for records, output and cancellation.

    The instance is resolved the way `job exec` resolves it (`worker`, or an exact `instance_name`, plus an
    optional `attempt`), and one command session is held for the client's lifetime:

        with JobExecdClient("job-abc", worker=0) as client:
            record = client.run(["python train.py"])
            final = client.follow(record["exec_id"], timeout=600, on_output=print)

    Construction is inert on purpose - no session, no install. The first call opens the session, installs or
    adopts the agent, and every later call reuses both.
    """

    def __init__(
        self,
        job_id: str,
        *,
        worker: int = 0,
        instance_name: str | None = None,
        attempt: int | None = None,
        wait_timeout: int = 0,
        web_session: Any | None = None,
        session: Any | None = None,
        agent_payload: bytes | None = None,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
        poll_interval: float = DEFAULT_FOLLOW_POLL,
        call_attempts: int = DEFAULT_CALL_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        share_session: bool = False,
    ) -> None:
        if not str(job_id or "").strip():
            raise ValueError("job_id is required")
        self.job_id = str(job_id)
        self.worker = int(worker)
        self.instance_name = instance_name
        self.attempt = attempt
        self.wait_timeout = int(wait_timeout)
        self.web_session = web_session
        self.command_timeout = int(command_timeout)
        self.poll_interval = max(0.0, float(poll_interval))
        self.call_attempts = max(1, int(call_attempts))
        self._sleep = sleep
        self._share_session = bool(share_session)
        self._agent_payload = agent_payload
        self._session = session
        #: True only when this client opened it, because a session handed in is the caller's to close.
        self._owns_session = session is None
        self._handle: AgentHandle | None = None
        # One namespace per client for the pod-side response spools. Random, not `id(self)`: the pod keys
        # pruning on this string, and Python reuses an object's address as soon as it is garbage collected, so
        # a second client built after the first was freed would inherit its namespace and prune away the spools
        # that prove the first client's RPCs already ran.
        self._namespace = f"client-{secrets.token_hex(8)}"

    # ---------------------------------------------------------------- session and install

    @property
    def agent(self) -> AgentHandle | None:
        """The agent this client drives, once a call has installed or adopted one."""
        return self._handle

    @property
    def session(self) -> Any | None:
        return self._session

    @property
    def instance_record(self) -> dict[str, Any] | None:
        """The platform's instance record, when the transport attached one."""
        record = getattr(self._session, "instance_record", None)
        return record if isinstance(record, dict) else None

    def _open_session(self) -> Any:
        if self._share_session:
            if self.web_session is not None:
                raise ValueError("a custom web_session cannot be delegated to the local broker")
            from inspire.config.accounts import get_account_override
            from inspire.platform.execd.session_broker import SharedJobSession

            override = get_account_override()
            return SharedJobSession(
                {
                    "job_id": self.job_id,
                    "worker": self.worker,
                    "instance_name": self.instance_name,
                    "attempt": self.attempt,
                    "wait_timeout": self.wait_timeout,
                    "account_alias": override.alias if override else None,
                }
            )
        return open_job_command_session(
            self.job_id,
            worker=self.worker,
            instance_name=self.instance_name,
            attempt=self.attempt,
            session=self.web_session,
            wait_timeout=self.wait_timeout,
        )

    def ensure(self) -> AgentHandle:
        """Open the session if needed and make sure our agent is the one answering. Idempotent."""
        if self._session is None:
            self._session = self._open_session()
            self._owns_session = True
        if self._handle is None:
            try:
                self._handle = ensure_agent(
                    self._session,
                    payload=self._agent_payload,
                    command_timeout=self.command_timeout,
                    sleep=self._sleep,
                )
            except ExecdError as exc:
                # A session that stopped answering would only be reopened by the next call anyway. A session
                # that answered fine and met a pod that cannot host execd (no python3, noexec /tmp) is worth
                # keeping: the caller's next move there is a report, not a retry.
                if exc.code in _TRANSPORT_CODES and self._owns_session:
                    self._drop_session()
                raise _redact_error(exc) from None
        return self._handle

    def _reopen_session(self) -> None:
        """Replace the session after a drop. The install is not redone: the daemon outlives the session."""
        self._drop_session()
        self._session = self._open_session()
        self._owns_session = True

    def _drop_session(self) -> None:
        session, self._session = self._session, None
        if session is not None and self._owns_session:
            try:
                session.close()
            except (
                Exception
            ):  # noqa: BLE001 - a session that dropped is exactly what we are replacing
                pass

    def close(self) -> None:
        """Close the command session if this client opened it. The pod-side daemon keeps running."""
        if self._owns_session:
            self._drop_session()

    def __enter__(self) -> "JobExecdClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- RPC plumbing

    def _call(
        self,
        method: str,
        args: dict[str, Any],
        *,
        wait_seconds: float = 0.0,
        deadline: float | None = None,
        wait_param: str | None = None,
    ) -> dict[str, Any]:
        """One RPC, with session establishment, bootstrap and transport re-asked on a fresh session.

        `deadline` is an absolute `time.monotonic()` value, and it bounds *this call's* waiting: each attempt
        long-polls for no longer than the time actually left, and the pause before the re-ask is clamped the
        same way. Without it a caller that asked for five seconds could still be waiting twenty after one lost
        answer, because the retry reused the original wait and the original retry delay. The re-ask itself is
        never dropped - a lost answer is exactly what the retry is for - and callers that are reading rather
        than waiting (the drain after a record goes terminal) pass no deadline.

        `wait_param` names the field in `args` that carries the long-poll to the daemon. The transport budget
        and the daemon's own wait have to be the same number on every attempt, so the value is stamped here,
        per attempt, rather than composed by the caller from a budget that is about to expire.
        """
        last: ExecdError | None = None
        for attempt in range(1, self.call_attempts + 1):
            wait = float(wait_seconds)
            if deadline is not None:
                wait = max(0.0, min(wait, deadline - time.monotonic()))
            call_args = args
            if wait_param is not None:
                call_args = dict(args)
                if wait > 0:
                    call_args[wait_param] = round(wait, 3)
                else:
                    call_args.pop(wait_param, None)
            try:
                self.ensure()
                session, handle = self._session, self._handle
                if (
                    session is None or handle is None
                ):  # pragma: no cover - ensure() raises before this
                    raise ExecdError("transport-lost", f"no exec session for {self.job_id}")
                return run_rpc(
                    session,
                    handle,
                    method,
                    call_args,
                    timeout=self.command_timeout,
                    wait_seconds=wait,
                    namespace=self._namespace,
                    sleep=self._sleep,
                )
            except (ExecdError, WebSocketException, requests.RequestException, OSError) as exc:
                if isinstance(exc, ExecdError):
                    if exc.code not in _TRANSPORT_CODES:
                        raise _redact_error(exc) from None
                    last = exc
                else:
                    last = ExecdError("transport-lost", f"{type(exc).__name__}: {exc}")
                if attempt >= self.call_attempts:
                    raise _redact_error(_with_attempts(last, attempt)) from None
                self._drop_session()
                delay = min(DEFAULT_RETRY_DELAY * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                self._sleep(delay)
        if last is None:  # pragma: no cover - the loop raises or returns before this
            raise ExecdError("transport-lost", f"execd {method} never reached the pod")
        raise _redact_error(last)  # pragma: no cover - the loop raises or returns first

    # ---------------------------------------------------------------- execd surface

    def health(self) -> dict[str, Any]:
        """Ask the daemon what it is. Also the cheapest way to prove the whole path works."""
        return self._call("health", {})

    def run(
        self,
        commands: str | Sequence[str],
        *,
        exec_id: str | None = None,
        session_name: str | None = None,
        session_bootstrap: str | None = None,
    ) -> dict[str, Any]:
        """Start a record and return the accepted record. This does not wait for the command.

        The exec id is generated *here* and sent with the request, which is what makes a re-ask safe: the agent
        recognises an id it has seen and answers with the record it already has (`created: false`), so "did my
        command start?" has one answer instead of "you now have two of them". Repeating `run` with a *new* id
        is a deliberate second execution.
        """
        record_id = validate_exec_id(exec_id) if exec_id else new_exec_id()
        args: dict[str, Any] = {"exec_id": record_id, "commands": _normalize_commands(commands)}
        if session_name is not None:
            args["session"] = validate_session(session_name)
        if session_bootstrap is not None:
            if session_name is None or not session_bootstrap.strip():
                raise ValueError("session_bootstrap requires a session and non-empty shell text")
            args["session_bootstrap"] = session_bootstrap
        data = self._call("run", args)
        _check_state(data, expected_id=record_id)
        if not str(data.get("exec_id") or ""):
            raise ExecdError(
                "protocol-mismatch",
                f"execd accepted a run without naming its exec id, so {record_id} cannot be followed",
                data={"requested_exec_id": record_id},
            )
        return data

    def attach(
        self,
        exec_id: str,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        max_bytes: int = DEFAULT_ATTACH_MAX_BYTES,
    ) -> dict[str, Any]:
        """Read an output page of a record. Read-only, so repeating it costs nothing but a round trip.

        Offsets are the contract: each page says where the next read should ask from, which is what lets
        `follow` resume across a reconnect without duplicating or skipping bytes. Asking for an offset past the
        end of a stream is an error the agent reports (`offset-out-of-range`) rather than a silent EOF, so a
        truncated remote log cannot masquerade as a finished command.
        """
        record_id = validate_exec_id(exec_id)
        return self._page(
            record_id,
            {"stdout": max(0, int(stdout_offset)), "stderr": max(0, int(stderr_offset))},
            max_bytes=int(max_bytes),
        )

    def _page(
        self,
        record_id: str,
        offsets: dict[str, int],
        *,
        max_bytes: int,
        wait_seconds: float = 0.0,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """One `attach` round trip, with the daemon's optional long-poll. Same page shape either way."""
        args: dict[str, Any] = {
            "exec_id": record_id,
            "stdout_offset": max(0, int(offsets["stdout"])),
            "stderr_offset": max(0, int(offsets["stderr"])),
            "max_bytes": max(0, int(max_bytes)),
        }
        data = self._call(
            "attach",
            args,
            wait_seconds=wait_seconds,
            deadline=deadline,
            wait_param="wait_seconds",
        )
        _check_state(data, expected_id=record_id)
        for stream in _STREAMS:
            window = _decode_window(data, stream, fallback_offset=int(offsets[stream]))
            # The base64 is dropped once decoded: keeping both would double the size of every page we hold,
            # and a follow that streams 40 MiB should not also carry 54 MiB of base64.
            data.pop(f"{stream}_b64", None)
            data[stream] = window
        return data

    def status(self, exec_id: str) -> dict[str, Any]:
        """One record's state, exit code and byte counts."""
        record_id = validate_exec_id(exec_id)
        data = self._call("status", {"exec_id": record_id})
        _check_state(data, expected_id=record_id)
        return data

    def list_records(self) -> list[dict[str, Any]]:
        """Every record the daemon knows about, including ones this client did not start."""
        data = self._call("list", {})
        records = data.get("records")
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ExecdError(
                "invalid-response", "execd returned something that is not a list of records"
            )
        return [dict(item) for item in records]

    def cancel(self, exec_id: str, grace_seconds: int = 5) -> dict[str, Any]:
        """Ask for a record to stop. A run that already finished is not an error - read `state`."""
        record_id = validate_exec_id(exec_id)
        data = self._call(
            "cancel", {"exec_id": record_id, "grace_seconds": max(0, int(grace_seconds))}
        )
        _check_state(data, expected_id=record_id)
        return data

    def cleanup(self, exec_id: str) -> dict[str, Any]:
        """Remove one terminal record's files. The agent refuses for a record still running."""
        record_id = validate_exec_id(exec_id)
        return self._call("cleanup", {"exec_id": record_id})

    def _page_across_drops(
        self,
        record_id: str,
        offsets: dict[str, int],
        *,
        max_bytes: int,
        wait_seconds: float,
        deadline: float | None,
        since: float,
        max_reconnects: int,
    ) -> tuple[dict[str, Any] | None, float | None]:
        """One page, asked again on a reopened session when the channel died and one call lost it.

        `_call` already re-asks a lost answer on a fresh session, so reaching here means the channel is gone
        rather than one answer missing. The record and its offsets live on the pod, so the page is asked
        again from the same two offsets: bytes already handed over stay handed over, later ones arrive on
        the new session, and the command is never sent again.

        Returns `(page, seconds)`, where `seconds` is how long the channel that just died had been carrying
        this read - the reader prints that, because a reader that resumes in silence makes the caller guess
        whether the gap was theirs. `(None, None)` means the caller's own deadline ran out with the channel
        down, which is the caller's stop and not a failure to report as the pod's. Once `max_reconnects`
        reconnects have failed in a row the last transport error is raised with these offsets attached.
        """

        def ask() -> dict[str, Any]:
            return self._page(
                record_id,
                offsets,
                max_bytes=max_bytes,
                wait_seconds=wait_seconds,
                deadline=deadline,
            )

        try:
            return ask(), None
        except ExecdError as exc:
            # A verdict about the record (`offset-out-of-range`, `exec-unknown`) is an answer, and a
            # reopened session would only be asked to give the same one again.
            if exc.code not in _TRANSPORT_CODES:
                raise
            last: BaseException = exc
        died_at = time.monotonic()
        failed = 0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return None, None
            if failed >= max(0, int(max_reconnects)):
                raise _redact_error(
                    _stopped_at(last, record_id=record_id, offsets=offsets, reconnects=failed)
                ) from None
            delay = min(DEFAULT_RETRY_DELAY * (2**failed), MAX_RETRY_DELAY)
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.monotonic()))
            self._sleep(delay)
            failed += 1
            try:
                # The install is not redone: the daemon outlived the channel that reached it.
                self._reopen_session()
                page = ask()
            except (ExecdError, WebSocketException, requests.RequestException, OSError) as exc:
                if isinstance(exc, ExecdError) and exc.code not in _TRANSPORT_CODES:
                    raise
                last = exc
                continue
            return page, max(0.0, died_at - since)

    def follow(
        self,
        exec_id: str,
        *,
        timeout: float | None = 60,
        from_offsets: Mapping[str, int] | None = None,
        max_bytes: int = DEFAULT_ATTACH_MAX_BYTES,
        on_output: Callable[[str, bytes], None] | None = None,
        on_reconnect: Callable[[dict[str, Any]], None] | None = None,
        max_reconnects: int = DEFAULT_MAX_RECONNECTS,
    ) -> dict[str, Any]:
        """Poll one record to a terminal state, streaming whatever it has produced.

        Every round trip is an `attach`, and each one asks the daemon to wait for output rather than returning
        immediately, so a quiet command costs a few round trips instead of one per second. A dropped session
        costs a reopen and a resume from the last verified offset: the command is never started again and no
        byte is delivered twice. A channel that dies outright - the platform closes these on a lifetime of
        its own - costs the same resume plus one call of `on_reconnect`, which is how a reader says out loud
        that it is still reading rather than letting the silence read as a failure. Ctrl-C is left alone on
        purpose - what an interrupt means (keep the record running, or cancel it) is a CLI decision.

        `timeout` bounds the *watch*, in seconds, and ``None`` bounds nothing: the read is already a string of
        bounded round trips, each long-polling for at most `MAX_FOLLOW_WAIT`, so following a command to its
        terminal state never means holding one call open past what the channel will tolerate. A number is for
        a caller that wants to stop watching at a chosen moment, and passing it costs nothing when the command
        has already finished - the drain past a terminal page is not cut short by it.

        `from_offsets` starts the read partway into the two logs, which is what makes a resumed watch a
        resume: a caller that already showed bytes 0..8122 asks from 8122 and sees only what came after. The
        offsets are per stream, so a resumed pair never repeats or skips a byte on either side.

        A terminal page is the record's last *state*, not the end of its output: a page carries at most
        `max_bytes` per stream, so a command that finished between two reads still owes the rest. The follow is
        done when the record is terminal *and* both streams report eof, which is why the drain past the terminal
        page costs no long-poll and is not cut short by `timeout` - that deadline bounds waiting for a command
        to finish, not how many pages its output needs.

        Returns the terminal record, or the last observed one with ``timed_out: true`` - including when the
        channel died and the caller's budget ran out before it came back, since stopping the watch is still
        not a verdict on the run. When the record did finish, a failed `status` read is reported under
        ``status_error`` rather than replacing the output already streamed. A channel that stayed down through
        `max_reconnects` reconnects raises the transport error, whose ``data`` carries the offsets this read
        stopped at and the number of reconnects it spent.
        """
        record_id = validate_exec_id(exec_id)
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        offsets = {"stdout": 0, "stderr": 0}
        for stream, value in dict(from_offsets or {}).items():
            if stream in offsets:
                offsets[stream] = max(0, int(value))
        record: dict[str, Any] = {}
        terminal = False
        quiet_drains = 0
        channel_since = time.monotonic()
        while True:
            wait_seconds = 0.0
            if not terminal:
                # A read always asks for a bounded wait, whether or not the caller named a budget; with a
                # budget it asks only for what is left of it, so one lost answer cannot overrun the deadline.
                wait_seconds = MAX_FOLLOW_WAIT
                if deadline is not None:
                    wait_seconds = min(wait_seconds, max(0.0, deadline - time.monotonic()))
            page, died_after = self._page_across_drops(
                record_id,
                offsets,
                max_bytes=max_bytes,
                wait_seconds=wait_seconds,
                # Only the waiting is bounded by the caller's timeout. Once the record is terminal the reads
                # that remain are pure reads, and stopping them at the deadline would report a truncated
                # output as a finished one.
                deadline=None if terminal else deadline,
                since=channel_since,
                max_reconnects=max_reconnects,
            )
            if page is None:
                # Out of budget with the channel down: where the bytes stopped stays true, and `timed_out`
                # below is the honest word for it. The record is on the pod either way.
                break
            if died_after is not None:
                channel_since = time.monotonic()
                if on_reconnect is not None:
                    # The offsets are the ones the resumed read asked from, which is what a caller who is
                    # watching the gap needs: not where the log is now, but where this reader never stopped.
                    on_reconnect(
                        {"exec_id": record_id, "elapsed": died_after, "offsets": dict(offsets)}
                    )
            gained = 0
            for stream in _STREAMS:
                window = page[stream]
                payload = window.get("data") or b""
                gained += len(payload)
                offsets[stream] = max(offsets[stream], int(window["next_offset"]))
                if payload and on_output is not None:
                    on_output(stream, payload)
            # The pages are handed to `on_output` and then dropped: a record that streamed 40 MiB should not come
            # back as a 40 MiB dict, and the offsets say where a later read picks up.
            record = {key: value for key, value in page.items() if key not in _STREAMS}
            if not terminal:
                terminal = page.get("terminal") is True or str(page.get("state")) in TERMINAL_STATES
            if terminal:
                if all(bool(page[stream]["eof"]) for stream in _STREAMS):
                    break
                quiet_drains = 0 if gained else quiet_drains + 1
                if quiet_drains > MAX_QUIET_DRAINS:
                    break
                continue
            if deadline is not None and time.monotonic() >= deadline:
                break
            self._sleep(self.poll_interval)

        result = {
            **record,
            "timed_out": not terminal,
            "stdout_offset": offsets["stdout"],
            "stderr_offset": offsets["stderr"],
        }
        if terminal:
            # The exit code lives on the record rather than in an output page, so a terminal follow asks for it.
            # If that read fails, the output already delivered is still what the caller wanted, so it is kept and
            # the failure is named instead of taking its place.
            try:
                result.update(self.status(record_id))
            except ExecdError as exc:
                result["status_error"] = exc.code
                result["status_error_message"] = str(exc)
        return result

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"JobExecdClient(job_id={self.job_id!r}, worker={self.worker}, "
            f"instance_name={self.instance_name!r}, attempt={self.attempt}, "
            f"agent={'ready' if self._handle else 'not yet'})"
        )


__all__ = ["DEFAULT_FOLLOW_POLL", "JobExecdClient"]
