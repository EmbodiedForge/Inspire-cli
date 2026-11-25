"""How ``inspire notebook exec`` gets work done and reads it back.

Every mode here exists because the Jupyter terminal is a channel this CLI does not own (see
``inspire/platform/web/browser_api/notebook_detach.py`` for the measurement). A command that runs
*in* that terminal comes down when the terminal is deleted, so no mode of this verb runs the command
there any more: the command is started on the notebook's own filesystem and read back from it. What
differs between the modes is who watches, and for how long.

Plain ``notebook exec`` starts the work and tails it back in one call: the launch is one short round
trip, then each read waits on the notebook for bytes so it costs one round trip per burst of output,
and a channel that dies mid-watch costs a reconnect. It stays with the exec until that exec reaches a
terminal state, which is what makes a long command survivable for a caller that never heard of
``--detach``; ``--timeout`` is how long a caller that cannot wait that long is willing to watch.
``--detach`` starts the same work and returns in seconds without watching at all. ``--attach`` answers
questions about a record from files, over a
fresh short-lived call every time, so it works from another process, after the process that started
it is gone, and after the terminal that launched it was deleted.

The exit codes follow ``inspire job exec`` v2, whose conventions this mirrors deliberately: the
command's own code when it finished, ``124`` when this process stopped watching an exec that is
still running (whatever the reason it stopped), ``14`` when the channel went before this call even
learned whether the command started, and the usual CLI codes for our own failures.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, NoReturn, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_TIMEOUT,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import notebook_detach as record_layout
from inspire.platform.web.browser_api.notebook_detach import (
    ATTACH_CHUNK_BYTES,
    AttachProtocolError,
    AttachWindow,
    ERROR_NO_SUCH_RECORD,
    ERROR_OFFSET_BEYOND_END,
    build_attach_command,
    build_detach_command,
    new_exec_id,
    new_read_tag,
    parse_attach_output,
    record_dir,
)
from inspire.platform.web.browser_api.notebook_shell import (
    NotebookShellChannelLost,
    NotebookShellTimeout,
    strip_terminal_framing,
)

#: "This process stopped watching an exec that is still running." Same number as ``job exec``'s
#: local timeout, same meaning: the exec is untouched, its outcome is not yet known, and the exec id
#: is the follow-up.
EXIT_STILL_RUNNING = 124

#: Seconds one observation call may take while ``--follow`` loops. Each call opens its own terminal,
#: so keeping them short is what makes a dead channel cost a retry instead of the whole budget.
ATTACH_CALL_TIMEOUT = 20
#: Pause between two observations when the last one produced nothing, so an idle follow does not
#: hammer the terminal. A read that delivered bytes is followed immediately.
ATTACH_POLL_SECONDS = 1.0
#: Most output one ``--json --follow`` call keeps before reporting the rest as resumable. Text mode
#: streams straight through and is not capped.
CAPTURE_MAX_BYTES = 1024 * 1024

#: Seconds one tail read may hold the channel. The terminal died under a single long call after
#: 253s-327s (see this module's sibling, ``notebook_detach``), so a read that stays inside two
#: minutes keeps that from being routine - and a read that does die is a retry, not a lost command.
TAIL_CALL_TIMEOUT = 120
#: How long that read waits *on the notebook* for bytes to arrive, returning the moment they do.
#: Waiting where the output is costs one round trip per burst of output instead of one per poll,
#: which is what makes a reconnecting tail feel live instead of batched.
TAIL_READ_WAIT_SECONDS = 90
#: Leave this much of the channel budget for the round trip itself, so the read answers on its own
#: rather than being overtaken by the transport's own deadline.
TAIL_CHANNEL_MARGIN_SECONDS = 5
#: Pause before issuing the next read when one came back empty while the exec is still running - a
#: command silent for the whole wait budget. A read that delivered bytes is followed immediately.
TAIL_POLL_SECONDS = 0.5
#: The call that starts the work. The launcher polls for up to two seconds for the detached shell to
#: record its pid, so this is generous rather than tight; a launch cut off mid-flight is the one
#: failure this CLI cannot describe, because it cannot say whether the command started.
LAUNCH_CALL_TIMEOUT = 60
#: The bound on one read when the caller set no watching budget at all. Nothing asks a read for more
#: than this: a watch with no deadline is a string of reads that each have one, never one long call
#: held open past the terminal's own patience (253-327s, measured) - which is the death this module
#: exists to survive, and a watch that courted it would be a worse version of the old CLI.
UNBOUNDED_CALL_TIMEOUT = 120
#: Reads in a row this call lets fail before it says the channel is gone for good. A watch that ends
#: on its own only ever ends on the record's terminal state, so with no budget to expire this is the
#: only thing between a caller and a notebook that was switched off mid-command.
MAX_LOST_READS = 3

#: One merged file, by construction: the command's stderr is redirected onto its stdout. Never
#: ``separate``, which would promise a caller that an empty stderr means nothing was written there.
STREAM_MODEL = "merged"
#: Same single merged stream, but the command had a terminal, so its program could see one: colours,
#: progress bars and line buffering all behave differently. Chosen by the runner and recorded in the
#: record, because the process reading the bytes back is usually not the one that started them.
PTY_STREAM_MODEL = "pty-merged"


def stream_model(pty: Optional[bool]) -> str:
    """The stream model a window's bytes must be reported under.

    ``None`` is a record that never said - ``--detach`` never asks, and an older client predates the
    marker - and such a record is a plain pipe, so it is reported as one rather than as something
    this call cannot tell.
    """
    return PTY_STREAM_MODEL if pty is True else STREAM_MODEL


def display_text(window: AttachWindow) -> str:
    """A window in the form its reader is meant to see.

    A PTY stream arrives with the terminal's own framing in it - ``\\r\\n`` line endings and the
    colour and cursor sequences the program emitted because it had a terminal. That is what the
    plain ``notebook exec`` of before always stripped on the way out (``notebook_shell._strip_ansi``),
    so a tail of a PTY record strips it too, and the same command's output does not change shape
    because the bytes now take a detour through a file. A pipe record is served verbatim: nothing
    rewrote those bytes, so there is nothing to undo, and ``stdout_b64`` carries the exact bytes of
    either kind.
    """
    text = window.text
    return strip_terminal_framing(text) if window.pty is True else text


def captured_text(captured: bytes, pty: Optional[bool]) -> str:
    """The bytes this call handed over, in the form a reader of ``stdout`` expects.

    ``stdout_b64`` beside it is always the exact bytes, which is what lets this field be the readable
    rendering: for a pipe record the two agree, and for a PTY record only this one is free of the
    terminal's framing - the same difference the terminal reader sees, published as a field instead
    of being hidden.
    """
    text = captured.decode("utf-8", errors="replace")
    return strip_terminal_framing(text) if pty is True else text


def _sleep(seconds: float) -> None:
    """The one place this module blocks the clock, so a test can step over it."""
    time.sleep(seconds)


def _dump(payload: dict[str, Any]) -> str:
    """The standard success envelope, forgiving about a detail this module did not foresee."""
    try:
        return json_formatter.format_json(payload)
    except TypeError:
        return json.dumps({"success": True, "data": payload}, indent=2, default=str)


def _resume_command(notebook_id: str, exec_id: str, offset: int, *, follow: bool) -> str:
    """The exact call that picks the record up where this one stopped."""
    tail = " --follow" if follow else ""
    return f"inspire notebook exec -n {notebook_id} --attach {exec_id} --after {offset}{tail}"


def _human_line(window: AttachWindow) -> str:
    state = f"{window.state} {window.exit_code}" if window.state == "exited" else window.state
    return (
        f"exec {window.exec_id}  {state}  "
        f"stdout {window.after}B -> {window.next_offset}B of {window.size}B"
    )


def _execute(
    *,
    notebook_id: str,
    command: str,
    session: Any,
    timeout: int,
    run_command_fn: Optional[Callable[..., dict[str, Any]]],
) -> Any:
    """One short-lived round trip over the terminal, on the same marker protocol as plain exec."""
    kwargs: dict[str, Any] = {
        "notebook_id": notebook_id,
        "command": command,
        "session": session,
        "timeout": timeout,
    }
    if run_command_fn is not None:
        kwargs["run_command_fn"] = run_command_fn
    return browser_api_module.execute_notebook_shell_command(**kwargs)


def _read_back(notebook_id: str, exec_id: str, directory: str) -> str:
    """How to find out what happened to a record this call cannot vouch for."""
    return (
        f"read it back with `inspire notebook exec -n {notebook_id} --attach {exec_id}`, or look "
        f"at the record itself with `inspire notebook exec -n {notebook_id} -- ls -l {directory}`"
    )


def _launch(
    *,
    exec_id: str,
    notebook_id: str,
    command: str,
    export_commands: tuple[str, ...],
    session: Any,
    timeout: int,
    pty: bool,
    run_command_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> Any:
    """Start one exec on the far side and hand back the launch call's result.

    The id is the caller's, minted before anything is sent, which is what makes even a failed launch
    answerable: the record directory it would have written is named in every error, so ``--attach``
    can tell the caller whether the command started after all.

    ``export_commands`` goes in front of the launching script rather than into the record, so the
    resolved ``[remote_env]`` prefix - which can hold secrets expanded from the local environment -
    is inherited by the detached process through the environment instead of being written to a file
    in the container that outlives this call.
    """
    script = build_detach_command(exec_id, command, pty=pty)
    launch = " && ".join([*export_commands, script]) if export_commands else script
    return _execute(
        notebook_id=notebook_id,
        command=launch,
        session=session,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )


def _launch_refused(
    ctx: Context,
    *,
    notebook_id: str,
    exec_id: str,
    directory: str,
    reason: str,
    hint_extra: str,
) -> None:
    """A launch that did not happen, reported with the one thing that can still answer for it."""
    _handle_error(
        ctx,
        "ExecError",
        f"Failed to start the command: {reason}",
        EXIT_API_ERROR,
        hint=f"{hint_extra}; {_read_back(notebook_id, exec_id, directory)}",
        data={"exec_id": exec_id, "record_dir": directory},
    )


def _refuse_launch(
    ctx: Context, *, notebook_id: str, exec_id: str, directory: str, cause: Exception
) -> None:
    """``--detach``'s answer to a launch call that raised: the id outranks the exception type."""
    _launch_refused(
        ctx,
        notebook_id=notebook_id,
        exec_id=exec_id,
        directory=directory,
        reason=str(cause),
        hint_extra="whether it started is unknown; its record directory would be that one",
    )


def _refuse_launch_rc(
    ctx: Context, *, notebook_id: str, exec_id: str, directory: str, returncode: int, detail: str
) -> None:
    """``--detach``'s answer to a launch the container itself refused."""
    _handle_error(
        ctx,
        "ExecError",
        "Failed to start the detached command: the notebook reported exit code "
        f"{returncode}" + (f": {detail}" if detail else ""),
        EXIT_API_ERROR,
        hint=(
            "nothing is running that this call started. The container may not allow writing "
            f"under {record_layout.RECORD_ROOT}; {_read_back(notebook_id, exec_id, directory)}"
        ),
        data={"exec_id": exec_id, "record_dir": directory},
    )


def run_detached_exec(
    ctx: Context,
    *,
    notebook_id: str,
    command: str,
    export_commands: tuple[str, ...] = (),
    session: Any,
    timeout: Optional[int],
    json_output: bool,
    run_command_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> None:
    """Start the command detached and return. Nothing here waits, so nothing here can time out.

    ``--detach`` asks for no PTY: it is the "I am not watching this at all" mode, and a caller that
    wants the work to outlive the call has no use for line-buffered progress output. The plain path
    that does watch asks for one; see :func:`run_streamed_exec`. The only thing ``timeout`` could
    bound here is the call that starts the work, which is why leaving it out takes the launch's own
    bound rather than a watching budget this mode does not have.
    """
    exec_id = new_exec_id()
    directory = record_dir(exec_id)
    try:
        result = _launch(
            exec_id=exec_id,
            notebook_id=notebook_id,
            command=command,
            export_commands=export_commands,
            session=session,
            timeout=LAUNCH_CALL_TIMEOUT if timeout is None else timeout,
            pty=False,
            run_command_fn=run_command_fn,
        )
    except Exception as exc:  # noqa: BLE001 - the id and the record path outrank the exception type
        _refuse_launch(
            ctx,
            notebook_id=notebook_id,
            exec_id=exec_id,
            directory=directory,
            cause=exc,
        )
        return

    returncode = int(getattr(result, "returncode", None) or 0)
    if returncode:
        detail = str(getattr(result, "stdout", "") or "").strip()
        _refuse_launch_rc(
            ctx,
            notebook_id=notebook_id,
            exec_id=exec_id,
            directory=directory,
            returncode=returncode,
            detail=detail,
        )

    payload: dict[str, Any] = {
        "mode": "detach",
        "notebook_id": notebook_id,
        "command": command,
        "exec_id": exec_id,
        "record_dir": directory,
        "stream_model": STREAM_MODEL,
        # One merged record: there is no separate stderr channel to report.
        "stderr": None,
        "state": "running",
        "detached": True,
        "next": _resume_command(notebook_id, exec_id, 0, follow=True),
    }
    if json_output:
        click.echo(_dump(payload))
        return
    click.echo(f"exec_id {exec_id}")
    click.echo(f"record_dir {directory}")
    click.secho(
        "detached: this process exiting does not touch the command, and neither does the terminal "
        f"channel dying. Read it with `inspire notebook exec -n {notebook_id} --attach {exec_id} "
        "--follow`.",
        dim=True,
        err=True,
    )


def _refuse_record(ctx: Context, notebook_id: str, window: AttachWindow, exec_id: str) -> None:
    """A record-level answer that is not an output window: no such exec, or an offset past its end."""
    root = record_layout.RECORD_ROOT
    if window.error == ERROR_NO_SUCH_RECORD:
        _handle_error(
            ctx,
            "ExecNotFound",
            f"No detached exec {exec_id} on notebook {notebook_id}.",
            EXIT_JOB_NOT_FOUND,
            hint=(
                f"records live under {root}/ on the notebook, so `inspire notebook exec -n "
                f"{notebook_id} -- ls {root}` lists the ids that exist there. A stopped "
                "notebook loses /tmp, and with it every record under it."
            ),
            data={"exec_id": exec_id},
        )
    if window.error == ERROR_OFFSET_BEYOND_END:
        _handle_error(
            ctx,
            "ValidationError",
            f"--after {window.after} is past the end of the record for {exec_id}, which holds "
            f"{window.size} bytes.",
            EXIT_VALIDATION_ERROR,
            hint=f"use the offset an earlier read reported, e.g. `--after {window.size}`",
            data={"exec_id": exec_id, "size": window.size},
        )
    _handle_error(
        ctx,
        "ExecError",
        f"The record for {exec_id} reported {window.error}.",
        EXIT_API_ERROR,
        hint=f"look at the record itself with `inspire notebook exec -n {notebook_id} -- ls -l {record_dir(exec_id)}`",
        data={"exec_id": exec_id, "error": window.error},
    )


def _report_attach(
    ctx: Context,
    *,
    notebook_id: str,
    window: Optional[AttachWindow],
    observations: int,
    observation_error: Optional[str],
    budget_expired: bool,
    watch_seconds: Optional[int],
    lost_reads: int,
    truncated: bool,
    captured: bytes,
    json_output: bool,
    follow: bool,
) -> None:
    """Print one attach's answer, then leave with the code that describes it."""
    if window is None:
        # Nothing was read, so nothing here may claim the exec exists, is running, or is gone: the
        # only honest answer is that the record could not be read, with the reason the read gave.
        detail = _because(observation_error)
        _handle_error(
            ctx,
            "ExecError",
            f"The detached exec's record could not be read at all.{detail}",
            EXIT_API_ERROR,
            hint=(
                "this call reads files on the notebook and runs nothing there, so nothing it did "
                "reaches the exec. Retry the same read; if it fails again the terminal, not the "
                "record, is the thing that is not answering."
            ),
        )

    payload: dict[str, Any] = {
        "mode": "attach",
        "notebook_id": notebook_id,
        "exec_id": window.exec_id,
        "record_dir": record_dir(window.exec_id),
        "stream_model": stream_model(window.pty),
        # The record merges both streams; null says "no separate stderr", not "empty".
        "stderr": None,
        "state": window.state,
        "exit_code": window.exit_code,
        # `job exec` publishes both spellings for one number, and so does this verb's plain path.
        "returncode": window.exit_code,
        "after": window.after,
        "offset": window.next_offset,
        "size": window.size,
        "more": window.more,
        "observations": observations,
        "truncated": truncated,
        "next": _resume_command(notebook_id, window.exec_id, window.next_offset, follow=follow),
    }
    if captured:
        payload["stdout"] = captured_text(captured, window.pty)
        # The exact bytes of the window, which ``stdout`` cannot promise for output that is not
        # text: a PTY-free record may hold anything, and a replacement character is not a byte.
        payload["stdout_b64"] = base64.b64encode(captured).decode("ascii")
    if observation_error:
        payload["observation_error"] = observation_error

    if json_output:
        click.echo(_dump(payload))
    else:
        click.secho(_human_line(window), dim=True, err=True)
        if window.more and not follow:
            click.secho(
                "more output is on the notebook than this call read: continue with "
                f"`{_resume_command(notebook_id, window.exec_id, window.next_offset, follow=follow)}`.",
                dim=True,
                err=True,
            )

    if window.state == "exited":
        code = int(window.exit_code or 0)
        if not json_output and code != 0:
            click.secho(
                f"the command exited {code}; its output is above, and the record stays at "
                f"{record_dir(window.exec_id)} until you remove it.",
                dim=True,
                err=True,
            )
        raise SystemExit(code)

    if window.state == "vanished":
        _handle_error(
            ctx,
            "ExecError",
            f"The detached exec {window.exec_id} is neither running nor finished: the recorded "
            "process is gone and no exit status was written.",
            EXIT_API_ERROR,
            hint=(
                "that is what a killed process, or a container that restarted, leaves behind. The "
                "output up to that point is still in "
                f"{record_dir(window.exec_id)}/stdout; nothing can tell you its exit code now."
            ),
            data={"exec_id": window.exec_id, "state": window.state, "offset": window.next_offset},
        )

    budget = f"{watch_seconds}s " if watch_seconds is not None else ""
    if observation_error or lost_reads:
        # The read's own complaint, which outranks the budget for the reason in `_stopped_watching`.
        message = (
            f"exec {window.exec_id} is still running; this call stopped watching because "
            f"{_failed_reads_phrase(lost_reads)} brought back no record"
            f"{_because(observation_error)}. The command runs on the notebook's own filesystem, not "
            "on that channel, and this call touched nothing."
        )
    elif budget_expired:
        # The caller's own bound expired, which is the one cause it can act on: the same command with
        # a longer --timeout, or none at all.
        message = (
            f"exec {window.exec_id} is still running and this call stopped watching when its own "
            f"{budget}watching budget ran out, after reading {window.next_offset}B of output."
        )
    else:
        message = (
            f"exec {window.exec_id} is still running. This call stopped watching it after reading "
            f"{window.next_offset}B of output, and touched nothing."
        )
    _handle_error(
        ctx,
        "ExecStillRunning",
        message,
        EXIT_STILL_RUNNING,
        hint=(
            "124 means this process stopped watching, not that anything failed. Keep watching with "
            f"`{_resume_command(notebook_id, window.exec_id, window.next_offset, follow=True)}`."
        ),
        data={
            "exec_id": window.exec_id,
            "state": window.state,
            "offset": window.next_offset,
            "more": window.more,
            "truncated": truncated,
        },
    )


def run_attach_exec(
    ctx: Context,
    *,
    notebook_id: str,
    exec_id: str,
    after: int,
    follow: bool,
    timeout: Optional[int],
    session: Any,
    json_output: bool,
    run_command_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> None:
    """Read a detached exec's record: once, or until it ends, or until the local budget runs out.

    Every observation opens its own terminal connection, so ``--follow`` survives a channel dying
    mid-watch: the bytes are counted in the notebook's own file, so re-opening the channel and
    reading from the last offset costs a retry and repeats nothing. A window is echoed only once it
    has parsed end to end, which is why a lost observation cannot duplicate output.

    ``--attach`` does not wait on the notebook: it is the report verb, asked once from a script, and
    an answer about a record is worth more than a fresh byte that arrives two minutes later. The
    plain path, which exists to watch, asks for the wait - see :func:`run_streamed_exec`. With
    ``--follow`` the same ``--timeout`` means the same thing it means everywhere else in this verb:
    how long this process is willing to watch, ``None`` being "until the exec is over".
    """
    watched = _watch_record(
        ctx,
        notebook_id=notebook_id,
        exec_id=exec_id,
        after=after,
        follow=follow,
        timeout=timeout,
        wait=0.0,
        call_timeout=ATTACH_CALL_TIMEOUT if follow else None,
        poll=ATTACH_POLL_SECONDS,
        session=session,
        json_output=json_output,
        run_command_fn=run_command_fn,
    )
    _report_attach(
        ctx,
        notebook_id=notebook_id,
        window=watched.last,
        observations=watched.observations,
        observation_error=watched.observation_error,
        budget_expired=watched.budget_expired,
        watch_seconds=watched.watch_seconds,
        lost_reads=watched.lost_reads,
        truncated=watched.truncated,
        captured=bytes(watched.captured),
        json_output=json_output,
        follow=follow,
    )


def _channel_notice(seconds: float, offset: int) -> str:
    """The one line a re-established channel owes the caller.

    Naming the elapsed time, the byte offset, and the fact that the command never noticed, in that
    order: the reader's first question is "did I lose something", the second is "did the command
    die", and the third is what to do if this call dies too.
    """
    return (
        f"the terminal channel closed after {int(round(max(seconds, 0.0)))}s; "
        f"the command is unaffected; resumed at offset {offset}"
    )


def _because(reason: Optional[str]) -> str:
    """A reason in parentheses, or nothing at all when it has nothing a reader can act on.

    A clause that trails off after a colon - ``(Completion marker not observed before timeout: )`` -
    reads as a truncated log line, and reads as a bug in the tool rather than in the notebook. A
    sentence that cannot name its cause is better off without the clause.
    """
    text = (reason or "").strip().rstrip(":;,")
    return f" ({text})" if text else ""


def _failed_reads_phrase(count: int) -> str:
    """How many reads this call gave up on, spelled the way the sentence needs it."""
    return "one read" if count <= 1 else f"{count} reads in a row"


def _no_pty_notice(directory: str) -> str:
    """The image could not give the command a terminal, and the caller is entitled to know."""
    return (
        f"no far-side PTY: this image has no working util-linux `script`, so the command runs on a "
        f"plain pipe (stream model {STREAM_MODEL}, not {PTY_STREAM_MODEL}). stdout and stderr are "
        "still one merged stream, but the program sees a pipe: no colours, and its own line "
        f"buffering may batch output. The record at {directory}/pty says which you got."
    )


def _read_budget(*, call_timeout: Optional[int], remaining: Optional[int]) -> int:
    """How long one read may hold the channel, given what is left of the caller's patience.

    The two bounds answer to each other: a read never outlives the budget it is watched under (a read
    the transport gave up on is a dead channel and a retry, where one that answered at its own
    deadline is an ordinary empty window), and a caller with no budget at all still gets reads that
    end, because it was the length of a single read that killed the old attached path.
    """
    if remaining is None:
        return int(call_timeout) if call_timeout is not None else UNBOUNDED_CALL_TIMEOUT
    if call_timeout is None:
        return max(int(remaining), 1)
    return max(min(int(call_timeout), int(remaining)), 1)


def _budget_ran_out(deadline: Optional[float]) -> bool:
    """Whether this process's watching budget is spent as of now."""
    return deadline is not None and time.monotonic() >= deadline


def _clock_ran_out(exc: Exception) -> bool:
    """Whether a failed read's own words are "my clock ran out" rather than "the channel died".

    The three exceptions a read can fail with, sorted by whose patience ended:

    * :class:`NotebookShellTimeout` - the transport said ``exitReason: deadline``, or the command
      echoed its start marker and then nothing. The read spent the time it was given.
    * :class:`NotebookShellChannelLost` - a subclass, because exit 14 already turns on the parent,
      but it means the opposite: the deadline had *not* expired. Excluded deliberately, not by luck
      of the ordering.
    * :class:`NotebookShellUnreachable` - the session never echoed a start marker and the transport
      gave no reason that means a clock, so this may not claim that the clock ran out.

    A read whose transport said ``deadline`` used to arrive here as ``NotebookShellUnreachable``
    carrying the transport's own "Completion marker not observed before timeout: " - the start marker
    had gone missing from the capture - and so a caller's spent budget was reported as a lost
    channel. ``notebook_shell`` now sorts that by the transport's reason, which is the answer,
    instead of by what survived in the buffer.
    """
    return isinstance(exc, NotebookShellTimeout) and not isinstance(exc, NotebookShellChannelLost)


def _channel_seconds(exc: Exception, opened: float) -> float:
    """How long a channel stayed open, from its own reading where it took one.

    The channel's reading beats this clock's, and the two are only ever close when the channel is
    what died (``notebook_shell`` takes the same view of the same number).
    """
    elapsed = getattr(exc, "elapsed_ms", None)
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
        return elapsed / 1000.0
    return time.monotonic() - opened


@dataclass
class _Watched:
    """What watching a record came to: the last answer, the bytes handed over, and why it stopped."""

    last: Optional[AttachWindow] = None
    observations: int = 0
    offset: int = 0
    captured: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    #: The failed read's own words, and only ever a *failed read*: a budget that expired is not a
    #: channel, and a caller that reads this field reads it as "the channel did something".
    observation_error: Optional[str] = None
    #: Set when the watch ended because this process's own budget ran out. Kept apart from
    #: ``observation_error`` because both can be true of one exit - the read that spent the whole
    #: budget reports its own deadline - and only one of them is the caller's doing.
    budget_expired: bool = False
    #: The watching budget this loop was built with, so the answer can name the number that ran out
    #: instead of pointing at a budget the caller cannot see.
    watch_seconds: Optional[int] = None
    #: Consecutive reads that failed to bring back a record, which is what a channel claim rests on.
    lost_reads: int = 0


def _watch_record(
    ctx: Context,
    *,
    notebook_id: str,
    exec_id: str,
    after: int,
    follow: bool,
    timeout: Optional[int],
    wait: float,
    call_timeout: Optional[int],
    poll: float,
    session: Any,
    json_output: bool,
    run_command_fn: Optional[Callable[..., dict[str, Any]]] = None,
    on_first_window: Optional[Callable[[AttachWindow], None]] = None,
) -> _Watched:
    """The loop both watching modes share: read a window, hand it over, repeat from the cursor.

    Two rules hold it together. The cursor advances only over bytes the caller was given, so a read
    that dies mid-flight costs a retry and repeats nothing. And a read that fails is an *answer* this
    loop turns into one of its own outcomes - never an exception it lets escape, because a caller
    that came to watch a command cannot act on a traceback, and the command is running regardless.
    Everything else is a knob: *timeout* is how long this process watches (``None`` until the record
    says it is over), *wait* is how long a read may block on the notebook for bytes to arrive (0
    answers at once), *call_timeout* caps one channel round trip (``None`` spends the whole remaining
    budget on it), *poll* is the pause after a read that came back empty while the exec is still
    running.
    """
    watched = _Watched(offset=int(after), watch_seconds=timeout)
    deadline = None if timeout is None else time.monotonic() + max(int(timeout), 1)
    #: A read that died is not reported until the channel comes back, because the honest sentence
    #: needs the resume offset and is worth nothing if nothing resumes.
    lost: Optional[tuple[float, str]] = None
    failed_reads = 0

    while True:
        tag = new_read_tag()
        # One reading of the clock per turn, reused for both the budget and the channel's age: a
        # watch is bounded by that budget, and a call that costs an extra tick each turn is a watch
        # that stops early.
        opened = time.monotonic()
        remaining = None if deadline is None else max(int(deadline - opened), 1)
        budget = _read_budget(call_timeout=call_timeout, remaining=remaining)
        # Never let the far-side wait outlive the channel that is carrying it: a read the transport
        # gave up on is a dead channel and a retry, where a read that answered at its own deadline
        # is an ordinary empty window.
        read_wait = max(min(wait, budget - TAIL_CHANNEL_MARGIN_SECONDS), 0.0) if wait else 0.0
        script = build_attach_command(
            exec_id,
            after=watched.offset,
            tag=tag,
            chunk=ATTACH_CHUNK_BYTES,
            wait=read_wait,
        )
        window: Optional[AttachWindow] = None
        try:
            shell = _execute(
                notebook_id=notebook_id,
                command=script,
                session=session,
                timeout=budget,
                run_command_fn=run_command_fn,
            )
            window = parse_attach_output(
                str(getattr(shell, "stdout", "") or ""), exec_id=exec_id, tag=tag
            )
        except AttachProtocolError as exc:
            # The answer arrived in no shape this module can honour. Retrying a shape problem reads
            # the same record the same way, so this is reported, not looped over.
            _handle_error(
                ctx,
                "ExecError",
                f"The record read for {exec_id} came back unusable ({exc.code}).",
                EXIT_API_ERROR,
                hint=(
                    "one read is one short command, so this is the terminal or the record - not a "
                    f"wait that expired. Retry `--attach {exec_id} --after {watched.offset}`, or "
                    f"look at {record_dir(exec_id)} on the notebook."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a failed read is an outcome, never a traceback
            # Which outcome depends on whose patience ended, not on what the read chose to call it.
            # A read that was allowed the whole remaining budget and came back saying it spent it
            # *is* this caller's budget expiring, whether this clock noticed first or that one did.
            whole_budget = remaining is not None and budget >= remaining
            channel_died = isinstance(exc, NotebookShellChannelLost)
            budget_spent = _budget_ran_out(deadline) or (whole_budget and _clock_ran_out(exc))
            if budget_spent and not channel_died:
                # The budget that ran out was this process's, and the channel is not known to have
                # done anything - so nothing is filed as a failed observation. A caller that reads
                # ``observation_error`` reads it as "the channel did something", and it did not.
                # A read that says the channel died is different: that is evidence about the
                # terminal, and a caller told to raise --timeout would be reaching for a knob that
                # cannot revive one, however spent its own budget is.
                watched.budget_expired = True
                watched.observation_error = None
                break
            failed_reads += 1
            watched.lost_reads = failed_reads
            if not follow or failed_reads >= MAX_LOST_READS:
                # Either this call asked for one read and got none, or the channel has failed this
                # many in a row and there is no budget left to expire that would ever end a watch.
                watched.observation_error = str(exc).strip() or None
                break
            lost = (_channel_seconds(exc, opened), str(exc).strip())

        if window is not None:
            if lost is not None:
                click.secho(_channel_notice(lost[0], watched.offset), dim=True, err=True)
                lost = None
            if watched.observations == 0 and on_first_window is not None:
                on_first_window(window)
            watched.observations += 1
            watched.observation_error = None
            watched.lost_reads = 0
            failed_reads = 0
            watched.last = window
            if window.error:
                _refuse_record(ctx, notebook_id, window, exec_id)
            watched.offset = window.next_offset
            if json_output:
                room = max(CAPTURE_MAX_BYTES - len(watched.captured), 0)
                if len(window.data) > room:
                    watched.truncated = True
                watched.captured.extend(window.data[:room])
            else:
                click.echo(display_text(window), nl=False)
            if window.finished or window.unresolved or watched.truncated:
                break
        elif lost is not None:
            watched.observation_error = lost[1]

        if not follow:
            break
        if _budget_ran_out(deadline):
            # This process's patience ended here, between two reads. A read that failed is left
            # standing as the reason if it was the last thing that happened: the budget expiring
            # beside a dead read does not un-kill it.
            watched.budget_expired = True
            break
        if window is None or not window.data:
            _sleep(poll)

    return watched


def _refuse_unconfirmed_launch(
    ctx: Context,
    *,
    notebook_id: str,
    exec_id: str,
    directory: str,
    exc: Exception,
) -> None:
    """The launch call died, so this call never learned whether there is a command to watch.

    That is a different answer from "the command failed", and it is not safe to guess between them:
    the command runs on the notebook's own filesystem, so the terminal dying under the launch may
    have killed nothing at all. Exit 14 with the channel's own words, the record path, and the one
    command that can tell the caller what actually happened.
    """
    channel = isinstance(exc, NotebookShellChannelLost)
    if channel:
        head = f"Command did not finish: {exc}"
        why = (
            "the channel closed, not the clock, so raising --timeout would not have helped. Whether "
            "the command started is unknown from here - it was already off the terminal, so this "
            "call dying answers nothing about it either way. "
        )
    else:
        head = f"Command did not finish within the timeout: {exc}"
        why = (
            "this client stopped waiting, and --timeout is the knob that decides when: the command "
            "no longer runs in the terminal, so that number bounds this process's watching and "
            "never the command. "
        )
    _handle_error(
        ctx,
        "ExecChannelLost" if channel else "ExecTimeout",
        head,
        EXIT_TIMEOUT,
        hint=(
            f"{why}Whether it started is one read away: "
            f"`{_read_back(notebook_id, exec_id, directory)}`. Do not simply re-run it if a second "
            "execution would do harm; --detach starts the same work without watching it at all."
        ),
        data={
            "exec_id": exec_id,
            "record_dir": directory,
            **(_channel_data(exc) if channel else {}),
        },
    )


def _channel_data(exc: Exception) -> dict[str, Any]:
    """What the transport knew about a channel that stopped answering, published as fields.

    The reason rides under ``exit_reason``, which is what this verb has published since the channel
    and the clock were separated; the transport's own attribute name is not the contract.
    """
    data: dict[str, Any] = {}
    reason = getattr(exc, "reason", None)
    if reason is not None:
        data["exit_reason"] = reason
    for name in ("elapsed_ms", "timeout"):
        value = getattr(exc, name, None)
        if value is not None:
            data[name] = value
    return data


def run_streamed_exec(
    ctx: Context,
    *,
    notebook_id: str,
    command: str,
    bridge: Optional[str] = None,
    export_commands: tuple[str, ...] = (),
    session: Any,
    timeout: Optional[int],
    json_output: bool,
    run_command_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> None:
    """Run the command on the notebook's own filesystem and stream its record back from here.

    This is what plain ``notebook exec`` does, and the measurement at the top of
    ``inspire/platform/web/browser_api/notebook_detach.py`` is why: a command left attached to the
    Jupyter terminal is killed when this client gives up on that terminal, so no mode of this verb
    runs the command there. The launch is one short call; the watching is a cursor into a file on
    the notebook. That makes the two ways this call can go wrong - the channel dies, or this process
    dies - both end the same way: the work is still there and one command picks it up.

    ``timeout`` is that watching budget, and it is the caller's, not the command's. Left unset the
    call follows the exec to its terminal state, which is what a caller that has never heard of
    ``--detach`` is asking for when it runs a long command; set, it is how long this process watches
    before it stops. Either way an exec this call stopped watching is untouched, and the answer is
    124 with the exec id and the byte offset to resume from.
    """
    started = time.monotonic()
    exec_id = new_exec_id()
    directory = record_dir(exec_id)
    try:
        result = _launch(
            exec_id=exec_id,
            notebook_id=notebook_id,
            command=command,
            export_commands=export_commands,
            session=session,
            timeout=(
                LAUNCH_CALL_TIMEOUT
                if timeout is None
                else max(min(int(timeout), LAUNCH_CALL_TIMEOUT), 1)
            ),
            pty=True,
            run_command_fn=run_command_fn,
        )
    except (NotebookShellChannelLost, NotebookShellTimeout) as exc:
        _refuse_unconfirmed_launch(
            ctx,
            notebook_id=notebook_id,
            exec_id=exec_id,
            directory=directory,
            exc=exc,
        )
        return
    except Exception as exc:  # noqa: BLE001 - the id and the record path outrank the exception type
        _refuse_launch(
            ctx,
            notebook_id=notebook_id,
            exec_id=exec_id,
            directory=directory,
            cause=exc,
        )
        return

    returncode = int(getattr(result, "returncode", None) or 0)
    if returncode:
        detail = str(getattr(result, "stdout", "") or "").strip()
        _refuse_launch_rc(
            ctx,
            notebook_id=notebook_id,
            exec_id=exec_id,
            directory=directory,
            returncode=returncode,
            detail=detail,
        )

    if not json_output:
        # Where the work lives, said once before any of its output: if this call is interrupted, or
        # the reader wants a second terminal, this is the line they come back to.
        click.secho(
            f"exec {exec_id}  record {directory}  "
            f"(read it from anywhere: inspire notebook exec -n {notebook_id} --attach {exec_id})",
            dim=True,
            err=True,
        )

    remaining = None if timeout is None else max(int(timeout - (time.monotonic() - started)), 1)

    def say_what_it_got(window: AttachWindow) -> None:
        # Only a record that says the image could not give a PTY earns that sentence. A window that
        # never got far enough to have one (missing, starting) says nothing about the image.
        if window.pty is False:
            click.secho(_no_pty_notice(directory), dim=True, err=True)

    watched = _watch_record(
        ctx,
        notebook_id=notebook_id,
        exec_id=exec_id,
        after=0,
        follow=True,
        timeout=remaining,
        wait=TAIL_READ_WAIT_SECONDS,
        call_timeout=TAIL_CALL_TIMEOUT,
        poll=TAIL_POLL_SECONDS,
        session=session,
        json_output=json_output,
        run_command_fn=run_command_fn,
        on_first_window=say_what_it_got,
    )
    _report_run(
        ctx,
        notebook_id=notebook_id,
        bridge=bridge,
        command=command,
        exec_id=exec_id,
        directory=directory,
        watched=watched,
        json_output=json_output,
    )


#: ``status`` answers the question a reader of the old payload asked - did the command work - and
#: ``state`` answers the one this transport makes worth asking: what does the record say now.
RUN_STATUS = {
    "exited": lambda code: "success" if not code else "failed",
    "running": lambda _code: "running",
    "vanished": lambda _code: "unresolved",
}
#: The state of a record this call started and never once read. Not ``running``, which would be a
#: guess about a file this process never opened, and not a new failure: the exec exists because this
#: call made it, and what it has done since is simply not known from here.
STATE_UNREAD = "unread"


def _stopped_watching(
    ctx: Context,
    *,
    exec_id: str,
    directory: str,
    watched: _Watched,
    resume: str,
    state: Optional[str] = None,
    more: Optional[bool] = None,
) -> NoReturn:
    """Leave the way every watch that ended early leaves: 124, and the cursor that picks it up.

    One exit however it was reached - the budget ran out, the channel would not come back, a read
    spent the whole remaining budget and said so - because the caller's next move is the same in
    every one of them: read the record back from the offset this call stopped at. The *sentence* is
    not the same and may not be, because the causes are: a caller that spent its own budget stopped
    watching, and a channel that died was replaced. A failed read outranks a spent budget, because a
    failed read is evidence about the terminal and a spent budget is evidence about this process; the
    record's state is named only when a read actually read it.
    """
    data: dict[str, Any] = {
        "exec_id": exec_id,
        "record_dir": directory,
        "offset": watched.offset,
        "truncated": watched.truncated,
        "next": resume,
    }
    if state is not None:
        data["state"] = state
    if more is not None:
        data["more"] = more
    budget = f"{watched.watch_seconds}s " if watched.watch_seconds is not None else ""
    if watched.observation_error or watched.lost_reads:
        # Something the terminal did, named by the read that felt it. This outranks the budget: a
        # dead channel beside a spent budget is still a dead channel, and "raise --timeout" is not a
        # remedy for one.
        cause = _because(watched.observation_error)
        failed = _failed_reads_phrase(watched.lost_reads)
        if state is not None:
            message = (
                f"exec {exec_id} is still running; this call stopped watching because {failed} "
                f"brought back no record{cause}. The command runs on the notebook's own filesystem, "
                "not on that channel, and this call touched nothing."
            )
        else:
            message = (
                f"exec {exec_id} was never read: {failed.lower()} brought back no record{cause}, so "
                "nothing here knows what the command has done. It runs on the notebook's own "
                "filesystem, not on that channel, and this call touched nothing."
            )
    elif watched.budget_expired:
        # The bound that expired was the caller's own. Naming it is the whole answer - the channel is
        # not known to have done anything, and a reader sent looking for a dead terminal finds one
        # that is not there.
        if state is not None:
            message = (
                f"exec {exec_id} is still running and this call stopped watching when its own "
                f"{budget}watching budget ran out, after reading {watched.offset}B of its output."
            )
        else:
            message = (
                f"exec {exec_id} is on the notebook and this call stopped watching when its own "
                f"{budget}watching budget ran out before any read of its record answered, so what "
                "it has produced so far is not known from here."
            )
    else:
        message = (
            f"exec {exec_id} is still running. This call stopped watching it after reading "
            f"{watched.offset}B of output, and touched nothing."
        )
    _handle_error(
        ctx,
        "ExecStillRunning",
        message,
        EXIT_STILL_RUNNING,
        hint=(
            "124 means this process stopped watching, not that anything failed. "
            + (
                "The command is untouched and keeps running. "
                if state is not None
                else "This call touched nothing; whether the command is running is one read away. "
            )
            + f"Keep watching with `{resume}`, or stop watching for good with --detach next time."
        ),
        data=data,
    )


def _report_run(
    ctx: Context,
    *,
    notebook_id: str,
    bridge: Optional[str],
    command: str,
    exec_id: str,
    directory: str,
    watched: _Watched,
    json_output: bool,
) -> None:
    """Print the plain exec's answer, then leave with the code that describes it."""
    window = watched.last
    resume = _resume_command(notebook_id, exec_id, watched.offset, follow=True)
    if window is None:
        # This call launched the record, so the command exists whether or not a read ever answered.
        # Whatever ran out was this process's patience, which the caller is owed as the same 124 and
        # the same cursor as any other expiry - an exception here is the one answer it cannot act on.
        unread: dict[str, Any] = {
            "mode": "run",
            "notebook_id": notebook_id,
            "bridge": bridge,
            "command": command,
            "exec_id": exec_id,
            "record_dir": directory,
            # Unknown, and published as unknown rather than as the plausible guess: no read reached
            # this record, so nothing here can say what stream it holds or how far it got.
            "stream_model": None,
            "stderr": None,
            "state": STATE_UNREAD,
            "status": STATE_UNREAD,
            "exit_code": None,
            "returncode": None,
            "offset": watched.offset,
            "size": None,
            "more": None,
            "observations": watched.observations,
            "truncated": watched.truncated,
            "next": resume,
        }
        if watched.observation_error:
            unread["observation_error"] = watched.observation_error
        if json_output:
            click.echo(_dump(unread))
        _stopped_watching(
            ctx,
            exec_id=exec_id,
            directory=directory,
            watched=watched,
            resume=resume,
        )

    code = int(window.exit_code or 0) if window.state == "exited" else None
    payload: dict[str, Any] = {
        "mode": "run",
        "notebook_id": notebook_id,
        "bridge": bridge,
        "command": command,
        "exec_id": exec_id,
        "record_dir": directory,
        # The record answers this, not the caller's guess: the process reading the bytes back is
        # usually not the one that started the command.
        "stream_model": stream_model(window.pty),
        # No stderr to assign. One merged stream by construction, whatever was underneath it - see
        # `display_text` for what is taken out of the text before it is handed over.
        "stderr": None,
        "state": window.state,
        "status": RUN_STATUS.get(window.state, lambda _code: window.state)(code),
        "exit_code": code,
        "returncode": code,
        "offset": watched.offset,
        "size": window.size,
        "more": window.more,
        "observations": watched.observations,
        "truncated": watched.truncated,
        "next": resume,
    }
    captured = bytes(watched.captured)
    if json_output and captured:
        payload["stdout"] = captured_text(captured, window.pty)
        # The exact bytes of the record, framing and all: `stdout` is the readable rendering, and a
        # caller that needs what is really in the file reads this.
        payload["stdout_b64"] = base64.b64encode(captured).decode("ascii")
    if watched.observation_error:
        payload["observation_error"] = watched.observation_error

    if json_output:
        click.echo(_dump(payload))
    elif window.state == "exited" and code:
        click.secho(
            f"the command exited {code}; its output is above, and the record stays at "
            f"{directory} until you remove it.",
            dim=True,
            err=True,
        )

    if window.state == "exited":
        raise SystemExit(code)

    if window.state == "vanished":
        _handle_error(
            ctx,
            "ExecError",
            f"The exec {exec_id} is neither running nor finished: the recorded process is gone "
            "and no exit status was written.",
            EXIT_API_ERROR,
            hint=(
                "that is what a killed process, or a container that restarted, leaves behind. The "
                f"output up to that point is still in {directory}/stdout; nothing can tell you its "
                "exit code now."
            ),
            data={"exec_id": exec_id, "state": window.state, "offset": watched.offset},
        )

    _stopped_watching(
        ctx,
        exec_id=exec_id,
        directory=directory,
        watched=watched,
        resume=resume,
        state=window.state,
        more=window.more,
    )
